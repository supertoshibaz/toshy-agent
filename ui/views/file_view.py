from shiboken6 import isValid
from ui.styles.binding import themed_color
from ui.styles.binding import watch_paint
from ui.document_paths import document_path, show_document_path
from PySide6.QtWidgets import QStyle, QStylePainter, QStyleOptionTab
from PySide6.QtGui import QPalette
from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.icons import set_icon, symbol_button
from ui.styles.binding import apply_style

from ui.file_saving import write_text
import os
import re
import hashlib
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Any

from ui.qt_deferred import defer

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QLabel, QToolButton, QPlainTextEdit, QLineEdit, QStackedWidget, QFrame, QTextEdit, QTabBar, QToolTip, QApplication, QMenu, QTextBrowser, QButtonGroup, QWidgetAction, QCheckBox, QPushButton, QSizePolicy, QFileDialog)
from ui.widgets.dialogs import MessageBox as QMessageBox
from PySide6.QtCore import (
    Qt, Signal, Slot, QRect, QRectF, QSize, QPointF, QTimer, QUrl, QPoint, QVariantAnimation, QEasingCurve,
    QFileSystemWatcher, QSaveFile, QIODevice, QObject, QEvent,
)
from ui.settings import QSettings
from PySide6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QPainterPath, QIcon,
    QTextFormat, QTextCharFormat, QTextOption,
    QTextCursor, QKeyEvent, QMouseEvent, QFocusEvent,
    QShortcut, QKeySequence
)

from ui.tab_memory import TabMemoryManager, SuspendedTab
from ui.event_bus import event_bus
from ui.app_state import app_state
from ui.highlighters import get_highlighter_for_file
from elements.lsp import lsp_manager
from .markdown_handler import MarkdownHandler
from .markdown_render import markdown_to_html
from .minimap import CodeMinimap
from .chat_view import TagsBarWidget
from ui.widgets.search_popup import SearchReplacePopup


EDITOR_READING_WIDTH = 960
EDITOR_MINIMAP_GAP = 10
EDITOR_LINE_NUMBER_GAP = 10


class _EditorIndentState(QObject):
    changed = Signal(bool)

    def __init__(self):
        super().__init__()
        settings = QSettings("Toshy", "Toshy")
        raw = settings.value("editor/use_tabs", True)
        self.use_tabs = raw if isinstance(raw, bool) else str(raw).lower() in {"1", "true", "yes"}

    def set_use_tabs(self, enabled: bool):
        enabled = bool(enabled)
        if self.use_tabs == enabled:
            return
        self.use_tabs = enabled
        QSettings("Toshy", "Toshy").setValue("editor/use_tabs", enabled)
        self.changed.emit(enabled)


EDITOR_INDENT_STATE = _EditorIndentState()


# =========================================================================
# КНОПКИ УПРАВЛЕНИЯ ВКЛАДКАМИ
# =========================================================================

class TabCloseButton(QToolButton):
    def __init__(self, parent=None):
        super().__init__(parent)
        watch_paint(self)
        self.setObjectName("TabCloseButton")
        self.setFixedSize(16, 16)
        self.setCursor(Qt.PointingHandCursor)
        set_text(self, 'ui.top_bar.close_a7a4', setter='setToolTip')

    def sizeHint(self) -> QSize:
        return QSize(16, 16)

    def minimumSizeHint(self) -> QSize:
        return QSize(16, 16)

    def paintEvent(self, event):
        from ui.icons import paint
        from ui.themes.manager import color
        painter = QPainter(self)
        box = QRectF((self.width()-16)/2, (self.height()-16)/2, 16, 16)
        if self.underMouse():
            painter.setPen(Qt.NoPen)
            painter.setBrush(color('control.hover'))
            painter.drawRoundedRect(box, 4, 4)
        paint(painter, box.adjusted(1, 1, -1, -1), 'close', enabled=self.isEnabled())


class NewTabButton(QToolButton):
    menu_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("NewTabButton")
        self.setFixedSize(20, 20)
        set_icon(self, 'add', square=True)
        self.setCursor(Qt.PointingHandCursor)
        set_text(self, 'ui.file_view.create_new_file_e92c', setter='setToolTip')
        self.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        self.clicked.connect(self.menu_requested)

    def mouseDoubleClickEvent(self, event):
        # Повторный щелчок не создаёт файл и не откладывает открытие меню.
        event.accept()


class TabRenameLineEdit(QLineEdit):
    editing_finished = Signal(str)
    cancelled = Signal()

    def __init__(self, text: str, parent=None):
        super().__init__(text, parent)
        self.setWindowFlags(Qt.SubWindow | Qt.FramelessWindowHint)
        apply_style(self, """
            QLineEdit {
                background-color: @background.input@;
                color: @text.primary@;
                border: 1px solid @accent.primary@;
                border-radius: 3px;
                padding: 1px 4px;
                font-size: 12px;
            }
        """)
        self.returnPressed.connect(self._on_return)

    def _on_return(self):
        self.editing_finished.emit(self.text().strip())
        self.deleteLater()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.cancelled.emit()
            self.deleteLater()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event):
        self.editing_finished.emit(self.text().strip())
        self.deleteLater()
        super().focusOutEvent(event)


class FileTabBar(QTabBar):
    def __init__(self, parent=None):
        super().__init__(parent)
        watch_paint(self)
        self.setObjectName("FileTabBar")
        self.setDrawBase(False)
        self.setFixedHeight(32)
        theme_manager.theme_changed.connect(self._theme_repaint)
        self.setUsesScrollButtons(True)
        self.setElideMode(Qt.ElideNone)
        self.setProperty('toshyCustomTabPaint', True)
        # Own the complete moving tab, including its document-state fill.
        self.setMovable(False)
        self._drag_press = None
        self._press_tab = -1
        self._drag_index = -1
        self._drag_label = None
        self._drag_close = None
        self._slide_offsets = {}
        self._slide_buttons = []
        self._slide_progress = 0.0
        self._slide_animation = QVariantAnimation(self)
        self._slide_animation.setDuration(130)
        self._slide_animation.setStartValue(1.0)
        self._slide_animation.setEndValue(0.0)
        self._slide_animation.setEasingCurve(QEasingCurve.OutCubic)
        self._slide_animation.valueChanged.connect(self._animate_slides)
        self._slide_animation.finished.connect(self._finish_slides)
        self._drag_watch = QTimer(self)
        self._drag_watch.setInterval(80)
        self._drag_watch.timeout.connect(self._check_drag_release)
        self.setExpanding(False)
        self.setIconSize(QSize(16, 16))
        self._rename_editor: Optional[TabRenameLineEdit] = None

    def setTabData(self, index, value):
        super().setTabData(index, value)
        if hasattr(value, 'state_changed'):
            try:
                value.state_changed.connect(self._refresh_document_states, Qt.UniqueConnection)
            except (TypeError, RuntimeError):
                pass
        self._refresh_tab_state(index)

    def setTabText(self, index, text):
        bind_caption(super(), text.removeprefix('* '), setter='setTabText', index=index)
        self._refresh_tab_state(index)

    def _refresh_tab_state(self, index):
        document = self.tabData(index)
        dirty = bool(getattr(document, 'is_dirty', getattr(document, 'dirty', False)))
        temporary = isinstance(document, TemporaryMarkdownTab)
        bind_caption(super(), self.tabText(index).removeprefix('* '), setter='setTabText', index=index)
        model = bool(getattr(document, '_model_modified', False))
        conflict = bool(getattr(document, '_external_conflict', False))
        self.setTabIcon(index, QIcon())
        indicator = self.tabButton(index, QTabBar.LeftSide)
        if indicator is not None:
            self.setTabButton(index, QTabBar.LeftSide, None)
            indicator.deleteLater()
        states = []
        if temporary: states.append(tr('ui.caption.532ee5bd9b8e'))
        if dirty: states.append(tr('ui.caption.c5805f8b9d79'))
        if model: states.append(tr('ui.caption.07f4bb23af5d') if conflict else tr('ui.caption.4face7c1a2c8'))
        elif conflict: states.append(tr('ui.caption.8241e0fcd41a'))
        if not states: states.append(tr('ui.caption.2fe8f2fd7243'))
        path = (getattr(document, 'file_path', '') or getattr(document, 'image_path', '')
                or getattr(document, 'title', ''))
        bind_caption(self, '\n'.join([str(path)] + states), setter='setTabToolTip', index=index)
        self.update()

    def _theme_repaint(self, *_):
        self.update()

    def showEvent(self, event):
        parent = self.parentWidget()
        layout = parent.layout() if parent is not None else None
        if layout is not None and layout.indexOf(self) >= 0:
            layout.setAlignment(self, Qt.AlignBottom)
        super().showEvent(event)

    def _tab_background(self, index):
        document = self.tabData(index)
        base = theme_manager.color('background.surface')
        dirty = bool(getattr(document, 'is_dirty', getattr(document, 'dirty', False)))
        model = bool(getattr(document, '_model_modified', False))
        conflict = bool(getattr(document, '_external_conflict', False))
        tint = ('#facc15' if dirty else '#f97316' if conflict else '#38bdf8' if model
                else '#a78bfa' if isinstance(document, TemporaryMarkdownTab) else None)
        if tint:
            status = QColor(tint)
            # The entire tab carries the status, with readable theme text.
            weight = .34 if theme_manager.current['foundation'] == 'dark' else .25
            base = QColor(*(round(a*(1-weight)+b*weight) for a,b in zip(base.getRgb()[:3],status.getRgb()[:3])))
        return base

    def paintEvent(self, event):
        painter = QStylePainter(self)
        try:
            right = self.findChild(QToolButton, 'ScrollRightButton')
            if right is not None and right.isVisible():
                painter.setClipRect(0, 0, max(0, right.x()), self.height())
            # Let Qt retain label/close-button geometry and native drag handling;
            # only the tab background and active outline are owned here.
            order = [i for i in range(self.count()) if i != self.currentIndex()]
            if self.currentIndex() >= 0:
                order.append(self.currentIndex())
            for index in order:
                if not self.isTabVisible(index) or (self._drag_label is not None and index == self._drag_index):
                    continue
                option = QStyleOptionTab()
                self.initStyleOption(option, index)
                rect = self.tabRect(index).translated(round(self._slide_offsets.get(index, 0)*self._slide_progress), 0)
                option.rect = rect
                painter.fillRect(rect, self._tab_background(index))
                outline = theme_manager.color('accent.primary' if index == self.currentIndex() else 'border.subtle')
                foreground = theme_manager.color('text.primary' if self.isTabEnabled(index) else 'text.disabled')
                option.palette.setColor(QPalette.WindowText, foreground)
                option.palette.setColor(QPalette.ButtonText, foreground)
                option.palette.setColor(QPalette.Text, foreground)
                painter.drawControl(QStyle.CE_TabBarTabLabel, option)
                painter.setPen(QPen(outline, 1))
                painter.setBrush(Qt.NoBrush)
                if rect.intersects(self.rect()):
                    painter.drawRect(QRectF(rect).adjusted(.5, .5, -.5, -.5))
        finally:
            painter.end()

    def _animate_slides(self, progress):
        self._slide_progress = float(progress)
        for button, position, offset in self._slide_buttons:
            if isValid(button):
                button.move(position + QPoint(round(offset*self._slide_progress), 0))
        self.update()

    def _finish_slides(self):
        self._slide_animation.stop()
        for button, position, _ in self._slide_buttons:
            if isValid(button):
                button.move(position)
        self._slide_buttons.clear()
        self._slide_offsets.clear()
        self._slide_progress = 0.0
        self.update()

    def _move_dragged_tab(self, target):
        source = self._drag_index
        boxes = [self.tabRect(i).translated(round(self._slide_offsets.get(i, 0)*self._slide_progress), 0)
                 for i in range(self.count())]
        self._finish_slides()
        order = list(range(self.count()))
        order.insert(target, order.pop(source))
        self._drag_index = target
        self.moveTab(source, target)
        for index, previous in enumerate(order):
            if index == target:
                continue
            offset = boxes[previous].x()-self.tabRect(index).x()
            if offset:
                self._slide_offsets[index] = offset
                for side in (QTabBar.LeftSide, QTabBar.RightSide):
                    button = self.tabButton(index, side)
                    if button is not None:
                        self._slide_buttons.append((button, button.pos(), offset))
        if self._slide_offsets:
            self._slide_animation.start()

    def mousePressEvent(self, event):
        self._end_drag()
        self._finish_slides()
        super().mousePressEvent(event)
        if event.button() == Qt.LeftButton:
            self._press_tab = self.tabAt(event.position().toPoint())
            self._drag_press = event.position().toPoint() if self._press_tab >= 0 else None
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_press is None or not event.buttons() & Qt.LeftButton:
            super().mouseMoveEvent(event)
            return
        position = event.position().toPoint()
        if self._drag_label is None:
            if (position-self._drag_press).manhattanLength() < QApplication.startDragDistance():
                return
            rect = self.tabRect(self._press_tab)
            pixmap = self.grab(rect)
            self._drag_hotspot = self._drag_press.x()-rect.x()
            self._drag_index = self._press_tab
            label = QLabel(self)
            label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            label.setStyleSheet('background:transparent;border:none;padding:0;')
            label.setPixmap(pixmap)
            label.resize(rect.size())
            self._drag_close = self.tabButton(self._drag_index, QTabBar.RightSide)
            if self._drag_close is not None:
                self._drag_close.hide()
            self._drag_label = label
            label.show()
            self._drag_watch.start()
            QToolTip.hideText()
        target = self.tabAt(position)
        if target >= 0 and target != self._drag_index:
            center = self.tabRect(target).center().x()
            if (target > self._drag_index and position.x() > center
                    or target < self._drag_index and position.x() < center):
                self._move_dragged_tab(target)
        x = max(0, min(position.x()-self._drag_hotspot, self.width()-self._drag_label.width()))
        self._drag_label.move(x, 0)
        self._drag_label.raise_()
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        dragging = self._drag_label is not None
        self._end_drag()
        if dragging:
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def _check_drag_release(self):
        if not QApplication.mouseButtons() & Qt.LeftButton:
            self._end_drag()

    def _end_drag(self):
        if not hasattr(self, '_drag_label'):
            return
        self._drag_watch.stop()
        if self._drag_label is not None:
            self._drag_label.hide()
            self._drag_label.deleteLater()
            self._drag_label = None
        if self._drag_close is not None and isValid(self._drag_close):
            self._drag_close.show()
        self._drag_close = None
        self._drag_index = -1
        self._press_tab = -1
        self._drag_press = None
        self.update()

    def event(self, event):
        if event.type() in (QEvent.Hide, QEvent.UngrabMouse, QEvent.WindowDeactivate):
            self._end_drag()
        return super().event(event)

    def tabRemoved(self, index):
        self._end_drag()
        self._finish_slides()
        super().tabRemoved(index)

    @Slot()
    def _refresh_document_states(self):
        for index in range(self.count()):
            self._refresh_tab_state(index)

    def tab_width(self) -> int:
        if self.count() > 0:
            w = self.tabRect(0).width()
            if w > 50:
                return w
        return 186

    def sizeHint(self) -> QSize:
        if self.count() == 0:
            return QSize(0, 32)
        return QSize(self.count() * self.tab_width(), 32)

    def minimumSizeHint(self) -> QSize:
        return QSize(48 if self.count() else 0, 32)

    def wheelEvent(self, event):
        delta = event.angleDelta().y() or event.angleDelta().x()
        if delta > 0:
            self.setCurrentIndex(max(0, self.currentIndex() - 1))
        elif delta < 0:
            self.setCurrentIndex(min(self.count() - 1, self.currentIndex() + 1))
        event.accept()

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            idx = self.tabAt(event.pos())
            if idx >= 0:
                self._start_tab_rename(idx)
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    def _start_tab_rename(self, idx: int):
        doc_tab = self.tabData(idx)
        if not doc_tab:
            return

        old_file_name = Path(doc_tab.file_path).name
        stem = Path(doc_tab.file_path).stem

        r = self.tabRect(idx)
        edit_rect = r.adjusted(6, 3, -24, -3)

        self._rename_editor = TabRenameLineEdit(old_file_name, self)
        self._rename_editor.setGeometry(edit_rect)
        self._rename_editor.show()
        self._rename_editor.setFocus()
        self._rename_editor.setSelection(0, len(stem))

        def on_finished(new_name: str):
            if not new_name or new_name == old_file_name:
                return

            old_path = doc_tab.file_path
            new_path = os.path.join(os.path.dirname(old_path), new_name)

            if os.path.exists(new_path) and new_path != old_path:
                return

            try:
                os.rename(old_path, new_path)
                event_bus.file_renamed.emit(old_path, new_path)
            except Exception as e:
                print(f"[FileView] Rename failed: {e}")

        self._rename_editor.editing_finished.connect(on_finished)


# =========================================================================
# РЕДАКТОР КОДА
# =========================================================================

class LineNumberArea(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        watch_paint(self)
        self.code_editor = editor
        self.setMouseTracking(True)

    def sizeHint(self) -> QSize:
        return QSize(self.code_editor.line_number_area_width(), 0)

    def paintEvent(self, event):
        self.code_editor.line_number_area_paint_event(event)

    def mouseMoveEvent(self, event):
        viewport_pos = self.code_editor.viewport().mapFromGlobal(
            event.globalPosition().toPoint()
        )
        cursor = self.code_editor.cursorForPosition(QPoint(0, viewport_pos.y()))
        message = self.code_editor._diagnostic_message_for_line(cursor.blockNumber())
        if message:
            QToolTip.showText(event.globalPosition().toPoint(), message, self)
            return
        QToolTip.hideText()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)



class MarkdownOutlineRail(QWidget):
    """Compact H1-H5 outline shown in Markdown's former line-number gutter."""

    ROW_HEIGHT = 14
    TOP_PAD = 7

    def __init__(self, editor):
        super().__init__(editor)
        watch_paint(self)
        self.editor = editor
        self._headings: List[Tuple[int, int]] = []
        self._enabled = False
        self._scroll_offset = 0
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self.setAttribute(Qt.WA_StyledBackground, True)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(140)
        self._refresh_timer.timeout.connect(self.refresh_headings)
        self.editor.document().contentsChange.connect(
            lambda _pos, removed, added: self.schedule_refresh() if removed or added else None)

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        if self._enabled:
            self.show()
            self.schedule_refresh()
        else:
            self.hide()
            self._headings.clear()
            self._scroll_offset = 0
            self.update()

    def schedule_refresh(self):
        if self._enabled:
            self._refresh_timer.start()

    def refresh_headings(self):
        provider = getattr(self.editor, 'heading_entries', None)
        if provider is not None:
            self._headings = list(provider())
            self._clamp_scroll()
            self.update()
            return
        headings: List[Tuple[int, int]] = []
        in_fence = False
        fence_token = None

        block = self.editor.document().firstBlock()
        while block.isValid():
            text = block.text()
            stripped = text.lstrip()

            if stripped.startswith("```") or stripped.startswith("~~~"):
                token = stripped[:3]
                if not in_fence:
                    in_fence = True
                    fence_token = token
                elif token == fence_token:
                    in_fence = False
                    fence_token = None
                block = block.next()
                continue

            if not in_fence:
                match = re.match(r"^\s*(#{1,5})(?!#)\s+\S", text)
                if match:
                    headings.append((block.blockNumber(), len(match.group(1))))

            block = block.next()

        self._headings = headings
        self._clamp_scroll()
        self.update()

    def _content_height(self) -> int:
        return self.TOP_PAD * 2 + len(self._headings) * self.ROW_HEIGHT

    def _max_scroll(self) -> int:
        return max(0, self._content_height() - max(1, self.height()))

    def _clamp_scroll(self):
        self._scroll_offset = max(0, min(self._scroll_offset, self._max_scroll()))

    def _row_y(self, index: int) -> float:
        return float(self.TOP_PAD + index * self.ROW_HEIGHT - self._scroll_offset + self.ROW_HEIGHT / 2)

    def _nearest_heading(self, y: float) -> Optional[int]:
        if not self._headings:
            return None
        best = None
        best_dist = self.ROW_HEIGHT * 0.52
        middle = int((y + self._scroll_offset - self.TOP_PAD) // self.ROW_HEIGHT)
        for index in range(max(0, middle - 1), min(len(self._headings), middle + 2)):
            block_number, _level = self._headings[index]
            row_y = self._row_y(index)
            if row_y < 0 or row_y > self.height():
                continue
            dist = abs(row_y - y)
            if dist <= best_dist:
                best_dist = dist
                best = block_number
        return best

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(event.rect(), theme_manager.color('background.window', shift=-0.02))
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.drawRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

        if not self._enabled or not self._headings:
            painter.end()
            return

        painter.setRenderHint(QPainter.Antialiasing, True)
        width = float(self.width())

        first = max(0, (self._scroll_offset - self.TOP_PAD) // self.ROW_HEIGHT - 1)
        last = min(len(self._headings), first + self.height() // self.ROW_HEIGHT + 4)
        for index in range(first, last):
            _block_number, level = self._headings[index]
            y = self._row_y(index)
            if y < -self.ROW_HEIGHT or y > self.height() + self.ROW_HEIGHT:
                continue

            # Compact tree row: "• ━━━". All levels use the same color; only
            # the horizontal indent communicates hierarchy. H5's dot sits
            # exactly at the rail center, with H1-H4 spread evenly before it.
            first_x = 3.5
            h5_x = width / 2.0
            indent_step = max(1.0, (h5_x - first_x) / 4.0)
            x = first_x + (level - 1) * indent_step
            branch_start = x + 4.5
            branch_end = max(branch_start + 4.0, width - 4.0)
            color = themed_color('text.secondary', 215)

            painter.setBrush(color)
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QPointF(x, y), 1.9, 1.9)
            painter.setPen(QPen(color, 1.4, Qt.SolidLine, Qt.RoundCap))
            painter.drawLine(QPointF(branch_start, y), QPointF(branch_end, y))

        painter.end()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._clamp_scroll()
        self.update()

    def wheelEvent(self, event):
        if not self._headings:
            event.ignore()
            return
        delta = event.angleDelta().y()
        if delta:
            steps = -1 if delta > 0 else 1
            self._scroll_offset += steps * self.ROW_HEIGHT * 3
            self._clamp_scroll()
            self.update()
            event.accept()
            return
        super().wheelEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        block_number = self._nearest_heading(event.position().y())
        self.setCursor(Qt.PointingHandCursor if block_number is not None else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.setCursor(Qt.ArrowCursor)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            block_number = self._nearest_heading(event.position().y())
            if block_number is not None:
                self.editor.navigate_to_heading(block_number)
                event.accept()
                return
        super().mousePressEvent(event)



class AgentSelectionPopup(QFrame):
    """Немодальный overlay для сборки одного запроса из фрагментов редактора."""

    submitted = Signal(str, object)
    cancelled = Signal()

    def __init__(self, editor, file_path_getter, parent=None, *, allow_file_path=True):
        super().__init__(parent or editor.viewport())
        self.editor = editor
        self._file_path_getter = file_path_getter
        self._allow_file_path = bool(allow_file_path)

        self.setObjectName("AgentSelectionPopup")
        self.setProperty("editorOverlay", True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        self.setMaximumWidth(430)
        self.resize(430, self.sizeHint().height())
        apply_style(self, """
            QFrame#AgentSelectionPopup {
                background-color: @background.panel@;
                border: 1px solid @border.subtle@;
                border-radius: 8px;
            }
            QLabel#AgentSelectionHint {
                color: @text.primary@;
                background: transparent;
                font-size: 11px;
            }
            QLabel#AgentSelectionSession {
                color: @text.disabled@;
                background: transparent;
                font-size: 10px;
            }
            QLineEdit#AgentSelectionInput {
                color: @text.primary@;
                background-color: @background.window@;
                border: 1px solid @border.subtle@;
                border-radius: 5px;
                padding: 6px 8px;
                selection-background-color: @control.selected@;
            }
            QLineEdit#AgentSelectionInput:focus {
                border-color: @text.disabled@;
            }
            QToolButton#AgentSelectionSend {
                color: @text.primary@;
                background-color: transparent;
                border: 1px solid @border.subtle@;
                border-radius: 6px;
                font-size: 14px;
                font-weight: bold;
            }
            QToolButton#AgentSelectionSend:hover {
                background-color: @control.hover@;
            }
            QToolButton#AgentSelectionClose {
                color: @text.secondary@;
                background: transparent;
                border: 1px solid @border.subtle@;
                border-radius: 4px;
                font-size: 15px;
                padding: 0;
            }
            QToolButton#AgentSelectionClose:hover {
                color: @text.primary@;
                background-color: @control.hover@;
            }
            QPushButton#ChatInputAuxBtn {border:1px solid @border.subtle@;border-radius:6px;background:transparent;}
            QPushButton#ChatInputAuxBtn:hover {background:@control.hover@;}
            QPushButton#ChatInputAuxBtn[active="true"] {background:@control.selected@;}
            QCheckBox#AgentSelectionPathToggle {
                color: @text.secondary@;
                background: transparent;
                font-size: 11px;
                spacing: 6px;
            }
            """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(6)

        self.hint_label = localize(QLabel(
            '',
            self,
        ), 'ui.file_view.select_text_fragments_to_add_them_b806')
        self.hint_label.setObjectName("AgentSelectionHint")
        self.hint_label.setWordWrap(True)
        # The hint is the popup's natural drag handle.  Let mouse events fall
        # through to AgentSelectionPopup instead of being swallowed by QLabel.
        self.hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header_row.addWidget(self.hint_label, 1)

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("AgentSelectionClose")
        set_icon(self.close_button, 'close', size=18)
        set_text(self.close_button, 'ui.top_bar.close_a7a4', setter='setToolTip')
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.setFixedSize(32, 32)
        self.close_button.clicked.connect(self.cancel)
        header_row.addWidget(self.close_button, 0, Qt.AlignTop | Qt.AlignRight)
        layout.addLayout(header_row)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)

        self.prompt_input = QLineEdit(self)
        self.prompt_input.setObjectName("AgentSelectionInput")
        set_text(self.prompt_input, 'ui.canvas_view.message_the_agent_2c9e', setter='setPlaceholderText')
        self.prompt_input.returnPressed.connect(self._submit)
        input_row.addWidget(self.prompt_input, 1)

        self.send_button = QToolButton(self)
        self.send_button.setObjectName("AgentSelectionSend")
        set_icon(self.send_button, 'send', size=20)
        self.send_button.setFixedSize(32, 32)
        set_text(self.send_button, 'ui.canvas_view.send_ceb6', setter='setToolTip')
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.clicked.connect(self._submit)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)

        bottom_row = QHBoxLayout()
        bottom_row.setContentsMargins(0, 0, 0, 0)
        bottom_row.setSpacing(8)

        self.path_toggle = QToolButton(self)
        self.path_toggle.setCheckable(True)
        set_icon(self.path_toggle,'file',size=18)
        set_text(self.path_toggle,'ui.file_view.include_file_path_977b',setter='setToolTip')
        self.path_toggle.setObjectName("AgentSelectionPathToggle")
        bottom_row.addWidget(self.path_toggle)
        self.path_toggle.setVisible(self._allow_file_path)
        self.whole_file_toggle = QToolButton(self)
        self.whole_file_toggle.setCheckable(True)
        set_icon(self.whole_file_toggle,'file-text',size=18)
        set_text(self.whole_file_toggle,'ui.file_view.include_entire_file_2a36',setter='setToolTip')
        set_text(self.whole_file_toggle, 'ui.file_view.include_all_current_text_including_unsaved_fd4f', setter='setToolTip')
        bottom_row.addWidget(self.whole_file_toggle)
        for toggle in (self.path_toggle,self.whole_file_toggle):
            toggle.setObjectName('AgentContextToggle')
            toggle.setFixedSize(32,32)
            apply_style(toggle,'QToolButton {background:transparent;border:1px solid @border.subtle@;border-radius:6px;padding:0;} QToolButton:hover {background:@control.hover@;} QToolButton:checked {background:@control.selected@;border-color:@accent.primary@;}')

        # Ровно та же TagsBarWidget, что используется в Chat. Обе панели
        # смотрят на единое shared state внутри chat_view, поэтому список тегов
        # и включенное/выключенное состояние синхронизируются мгновенно.
        from ui.views.chat_input_controls import ChatIconButton
        self.tags_button = ChatIconButton('tag', tr('ui.canvas_view.tags_1331'), self)
        self.tags_button.setObjectName("ChatInputAuxBtn")
        self.tags_button.setCursor(Qt.PointingHandCursor)
        self.tags_button.clicked.connect(self._toggle_tags_panel)
        bottom_row.addWidget(self.tags_button)

        self.clear_selection_button = ChatIconButton('backspace', tr('ui.file_view.clear_all_marked_fragments_0d2e'), self)
        self.clear_selection_button.setObjectName("ChatInputAuxBtn")
        self.clear_selection_button.setCursor(Qt.PointingHandCursor)
        set_text(self.clear_selection_button, 'ui.file_view.clear_all_marked_fragments_0d2e', setter='setToolTip')
        self.clear_selection_button.clicked.connect(self.editor.clear_agent_context)
        bottom_row.addWidget(self.clear_selection_button)
        bottom_row.addStretch(1)

        self.session_label = QLabel(self)
        self.session_label.setObjectName("AgentSelectionSession")
        bottom_row.addWidget(self.session_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(bottom_row)

        app_state.session_changed.connect(self._refresh_session_label)
        app_state.titles_changed.connect(self._catalog_title_changed)
        self._refresh_session_label(app_state.session_id or "", app_state.branch)

        # Overlay над основным popup. Это отдельный sibling внутри viewport,
        # поэтому открытие тегов не меняет размер/разметку самого виджета.
        self.tags_bar = TagsBarWidget(self.parentWidget())
        self.tags_bar.installEventFilter(self)
        self.tags_bar.tag_state_changed.connect(lambda: defer(0, self, self._position_tags_bar))
        self.tags_bar.hide()
        self.tags_button.set_active(False)

        self._drag_start_global: Optional[QPoint] = None
        self._drag_start_pos: Optional[QPoint] = None

        # QLineEdit consumes key events while it owns focus, so handling Escape
        # only in QFrame.keyPressEvent is not enough.  This shortcut applies to
        # the popup and all of its children.
        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.cancel)
        self.hide()

    def _catalog_title_changed(self):
        self._refresh_session_label(app_state.session_id, app_state.branch)

    def _refresh_session_label(self, session_id: str, _branch: str = "main"):
        full_label = app_state.display_session(session_id, _branch)
        visible_label = full_label if len(full_label) <= 22 else full_label[:19] + "…"
        set_text(self.session_label, 'ui.canvas_view.session_v0_7b37', v0=visible_label)
        bind_caption(self.session_label, full_label, setter='setToolTip')

    def _toggle_tags_panel(self):
        will_show = not self.tags_bar.isVisible()
        self.tags_bar.setVisible(will_show)
        self.tags_button.set_active(will_show)
        if will_show:
            self._position_tags_bar(adjust_popup=True)
            defer(0, self, lambda: self._position_tags_bar(adjust_popup=True))

    def _position_tags_bar(self, *, adjust_popup: bool = False):
        if not self.tags_bar.isVisible():
            return
        parent = self.parentWidget()
        if parent is None:
            return

        width = self.width()
        layout_height = self.tags_bar.flow_layout.heightForWidth(width)
        panel_height = max(36, min(170, layout_height))
        gap = 8

        if adjust_popup:
            desired_top = self.y() - panel_height - gap
            if desired_top < 6:
                needed = 6 - desired_top
                max_popup_y = max(6, parent.height() - self.height() - 6)
                new_y = min(max_popup_y, self.y() + needed)
                if new_y != self.y():
                    self.move(self.x(), new_y)

        x = max(0, min(self.x(), max(0, parent.width() - width)))
        y = max(0, self.y() - panel_height - gap)
        self.tags_bar.setGeometry(x, y, width, panel_height)
        self.tags_bar.show()
        self.raise_()
        self.tags_bar.raise_()

    @staticmethod
    def _fenced_fragment(text: str) -> str:
        runs = re.findall(r"`+", text or "")
        longest = max((len(run) for run in runs), default=0)
        fence = "`" * max(3, longest + 1)
        return f"{fence}\n{text}\n{fence}"

    def _workspace_relative_path(self) -> str:
        """User-facing path: <workspace-folder>/<path-inside-workspace>."""
        file_path = Path(self._file_path_getter()).resolve()
        workspace = Path(app_state.workspace).resolve()
        try:
            rel = file_path.relative_to(workspace).as_posix()
        except ValueError:
            rel = Path(os.path.relpath(file_path, workspace)).as_posix()
        workspace_name = workspace.name or "workspace"
        return f"{workspace_name}/{rel.lstrip('/')}"

    def _model_file_path(self) -> str:
        return str(Path(self._file_path_getter()).resolve())

    def build_file_context(self) -> Dict[str, Any]:
        if not self._allow_file_path or not self.path_toggle.isChecked():
            return {}
        return {
            "includePath": True,
            "displayPath": self._workspace_relative_path(),
            "modelPath": self._model_file_path(),
        }

    def build_message(self) -> str:
        sections: List[str] = []
        if self._allow_file_path and self.path_toggle.isChecked():
            sections.append(tr('ui.caption.6747b9e0a093', v0=self._workspace_relative_path()))

        # Context always precedes the actual user request in the Chat message.
        # The popup input itself still contains only the user's prompt.
        fragments = ([self.editor.toPlainText()] if self.whole_file_toggle.isChecked()
                     else self.editor.agent_context_fragments())
        sections.extend(self._fenced_fragment(fragment) for fragment in fragments)

        prompt = self.prompt_input.text().strip()
        if prompt:
            sections.append(prompt)

        return "\n\n".join(section for section in sections if section)

    def open_at(self, anchor: QPoint):
        parent = self.parentWidget()
        if parent is None:
            return
        self.adjustSize()
        width = min(430, max(280, parent.width() - 12))
        self.resize(width, self.sizeHint().height())

        gap = 7
        x = anchor.x() + gap
        y = anchor.y() + gap
        if x + self.width() > parent.width() - 6:
            x = max(6, anchor.x() - self.width() - gap)
        if y + self.height() > parent.height() - 6:
            y = max(6, anchor.y() - self.height() - gap)
        self.move(max(6, x), max(6, y))
        self.show()
        self.raise_()
        self.prompt_input.setFocus()

    def reset_and_hide(self):
        self.prompt_input.clear()
        self.path_toggle.setChecked(False)
        self.whole_file_toggle.setChecked(False)
        self.tags_bar.hide()
        self.tags_button.set_active(False)
        self.hide()

    def cancel(self):
        self.reset_and_hide()
        self.cancelled.emit()

    def _submit(self):
        message = self.build_message().strip()
        if message:
            self.submitted.emit(message, self.build_file_context())

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
        if watched is getattr(self, 'tags_bar', None) and event.type() in (QEvent.Show, QEvent.Hide):
            self.tags_button.set_active(event.type() == QEvent.Show)

        if event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape and self.isVisible():
            self.cancel()
            return True
        return False

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() == Qt.Key_Escape:
            self.cancel()
            event.accept()
            return
        super().keyPressEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            # Interactive controls keep their normal behaviour; dragging starts
            # from the popup background / labels, like a small floating panel.
            target = self.childAt(event.position().toPoint())
            interactive = isinstance(target, (QLineEdit, QToolButton, QCheckBox))
            if not interactive:
                self._drag_start_global = event.globalPosition().toPoint()
                self._drag_start_pos = self.pos()
                self.raise_()
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_start_global is not None and self._drag_start_pos is not None:
            delta = event.globalPosition().toPoint() - self._drag_start_global
            candidate = self._drag_start_pos + delta
            parent = self.parentWidget()
            if parent is not None:
                max_x = max(0, parent.width() - self.width())
                max_y = max(0, parent.height() - self.height())
                candidate.setX(max(0, min(candidate.x(), max_x)))
                candidate.setY(max(0, min(candidate.y(), max_y)))
            self.move(candidate)
            self._position_tags_bar()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        defer(0, self, self._position_tags_bar)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        defer(0, self, self._position_tags_bar)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._drag_start_global is not None:
            self._drag_start_global = None
            self._drag_start_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

class VSCodeEditor(QPlainTextEdit):
    _text_boundary_in_key_handler = True
    agent_popup_requested = Signal(QPoint)

    def __init__(self, parent=None):
        super().__init__(parent)
        watch_paint(self)
        self.setObjectName("VSCodeEditor")
        theme_manager.theme_changed.connect(self._refresh_theme)
        self._content_revision = 0
        self._decorations_key = None
        self._decoration_revision = 0
        from .editor_diagnostics import DiagnosticIndex
        self._diagnostic_index = DiagnosticIndex()
        self._diagnostic_selection_key = None
        self._diagnostic_selections = []
        self._visible_decorations_timer = QTimer(self)
        self._visible_decorations_timer.setSingleShot(True)
        self._visible_decorations_timer.timeout.connect(self._update_decorations)
        self._wrap_layout_key = None
        self._forced_wrap_key = None
        self._wrap_sync_timer = QTimer(self)
        self._wrap_sync_timer.setSingleShot(True)
        self._wrap_sync_timer.timeout.connect(self._sync_wrap_layout)
        self._horizontal_reset_timer = QTimer(self)
        self._horizontal_reset_timer.setSingleShot(True)
        self._horizontal_reset_timer.timeout.connect(self._reset_wrapped_horizontal_offset)

        base_font = QFont(QApplication.font())
        base_font.setPointSize(11)
        self.setFont(base_font)
        self.document().setDefaultFont(base_font)

        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(' ') * 4)
        self.document().setDocumentMargin(4)
        self.setCenterOnScroll(True)
        self.setMouseTracking(True)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        # Keep literal tabs as real ``\t`` characters.  Their layout uses
        # Qt's native tab-stop grid (4 spaces), while the visible marker is
        # painted separately.  This mirrors conventional editors and avoids
        # per-paragraph tab-position state becoming stale while typing.

        self._agent_context_active = False
        self._agent_context_cursors: List[QTextCursor] = []
        self._agent_left_mouse_down = False
        # During Shift+keyboard selection we continuously preview XOR against
        # the context state that existed when the gesture began.  Without a
        # stable base, every repeated Shift+Arrow would XOR the growing
        # selection again and produce alternating/ragged marked ranges.
        self._agent_keyboard_context_base: Optional[List[Tuple[int, int]]] = None


        # Global indentation mode shared by every open file.
        self.use_tabs = bool(EDITOR_INDENT_STATE.use_tabs)
        EDITOR_INDENT_STATE.changed.connect(self._set_global_indent_mode)

        self._search_ranges: List[Tuple[int, int]] = []
        self._search_current_range: Optional[Tuple[int, int]] = None
        self._setting_plain_text = False

        self.line_number_area = LineNumberArea(self)
        self.minimap = CodeMinimap(self)
        self.outline_rail = MarkdownOutlineRail(self)
        self.outline_rail.hide()
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.verticalScrollBar().valueChanged.connect(self.minimap.update)

        self._heading_flash_block: Optional[int] = None
        self._heading_flash_timer = QTimer(self)
        self._heading_flash_timer.setSingleShot(True)
        self._heading_flash_timer.setInterval(900)
        self._heading_flash_timer.timeout.connect(self._clear_heading_flash)

        self.is_markdown = False
        self.markdown_handler = MarkdownHandler(self)

        self.diagnostics: List[Dict[str, Any]] = []
        self.line_status_map: Dict[int, str] = {}

        self.suppressed_line: Optional[int] = None
        self._last_cursor_line: int = 0
        self._typing_idle_timer = QTimer(self)
        self._typing_idle_timer.setSingleShot(True)
        self._typing_idle_timer.setInterval(1000)
        self._typing_idle_timer.timeout.connect(self._on_typing_idle)

        self.blockCountChanged.connect(self.update_line_number_area_width)
        self.updateRequest.connect(self.update_line_number_area)
        self.cursorPositionChanged.connect(self._on_cursor_position_changed)
        self._document_change_pending = False
        self._decoration_timer = QTimer(self)
        self._decoration_timer.setSingleShot(True)
        self._decoration_timer.timeout.connect(self._finish_document_change)
        self.document().contentsChange.connect(self._on_document_contents_changed)

        self.update_line_number_area_width(0)
        self._update_decorations()

    def set_file_path(self, file_path: str):
        self.file_path = file_path
        ext = Path(file_path).suffix.lower()
        self.is_markdown = ext in {".md", ".markdown"}
        self.markdown_handler.set_enabled(self.is_markdown)
        self.markdown_handler.set_visual_enabled(self.is_markdown)

        # 1. Для Markdown: мягкий перенос строк и скрытие панели номеров
        if self.is_markdown:
            self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.horizontalScrollBar().setValue(0)
            self.line_number_area.hide()
            self.outline_rail.set_enabled(True)
        # 2. Для кода: строгая строка без переноса и видимая панель номеров
        else:
            self.setLineWrapMode(QPlainTextEdit.NoWrap)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            self.line_number_area.show()
            self.outline_rail.set_enabled(False)

        self.update_line_number_area_width(0)
        self.viewport().update()
        self._schedule_wrap_sync()

    def _on_document_contents_changed(self, position, removed, added):
        if removed or added:
            self._content_revision += 1
            self._document_change_pending = True
            self._search_ranges = []
            self._search_current_range = None
            self._decoration_timer.start(0)

    def _finish_document_change(self):
        self._document_change_pending = False
        self._on_editor_text_changed()

    def _schedule_wrap_sync(self):
        if not self._wrap_sync_timer.isActive(): self._wrap_sync_timer.start(0)

    def _schedule_horizontal_reset(self):
        if not self._horizontal_reset_timer.isActive(): self._horizontal_reset_timer.start(0)

    def _on_editor_text_changed(self):
        if self.lineWrapMode() != QPlainTextEdit.NoWrap:
            # QPlainTextEdit may run ensureCursorVisible *after* textChanged.
            # Reset both now and on the next event-loop turn so wrapped text
            # can never retain a hidden horizontal pan.
            self.horizontalScrollBar().setValue(0)
            self._schedule_horizontal_reset()

        curr_line = self.textCursor().blockNumber()
        self.suppressed_line = curr_line
        self._last_cursor_line = curr_line
        self._typing_idle_timer.start()

        if self.is_markdown:
            self.markdown_handler.parse_code_blocks()

        self._update_decorations()
        if self._agent_context_active:
            self._normalize_agent_context_cursors()

    def _on_cursor_position_changed(self):
        curr_line = self.textCursor().blockNumber()
        if curr_line != self._last_cursor_line:
            self.suppressed_line = None
            self._typing_idle_timer.stop()
            self._last_cursor_line = curr_line

        self._update_decorations()

    def activate_agent_context(self, anchor: QPoint, *, include_current_selection: bool = True):
        intervals: List[Tuple[int, int]] = []
        cursor = self.textCursor()
        if include_current_selection and cursor.hasSelection():
            intervals.append((cursor.selectionStart(), cursor.selectionEnd()))

        self._agent_context_active = True
        self._set_agent_context_intervals(intervals)
        self.agent_popup_requested.emit(QPoint(anchor))

    def clear_agent_context(self):
        """Clear marked agent-context fragments without leaving selection mode."""
        self._agent_context_cursors.clear()
        self._agent_keyboard_context_base = None
        self._update_decorations()

    def cancel_agent_context(self):
        self._agent_context_active = False
        self.clear_agent_context()

    def agent_context_fragments(self) -> List[str]:
        fragments: List[str] = []
        for start, end in self._agent_context_intervals():
            cursor = QTextCursor(self.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            fragment = self.markdown_handler.selected_source_text(cursor)
            # Ignore context ranges that contain whitespace only. The range may
            # remain visually marked in the editor, but it must never create an
            # empty fenced block in the Chat message.
            if fragment.strip():
                fragments.append(fragment)
        return fragments

    def _agent_context_intervals(self) -> List[Tuple[int, int]]:
        intervals: List[Tuple[int, int]] = []
        for cursor in self._agent_context_cursors:
            start, end = cursor.selectionStart(), cursor.selectionEnd()
            if end > start:
                intervals.append((start, end))
        return sorted(intervals)

    def _set_agent_context_intervals(self, intervals: List[Tuple[int, int]]):
        self._agent_context_cursors = []
        doc = self.document()
        max_pos = max(0, doc.characterCount() - 1)
        for start, end in sorted(intervals):
            start = max(0, min(int(start), max_pos))
            end = max(0, min(int(end), max_pos))
            if end <= start:
                continue
            cursor = QTextCursor(doc)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.KeepAnchor)
            self._agent_context_cursors.append(cursor)
        self._update_decorations()

    def _normalize_agent_context_cursors(self):
        self._set_agent_context_intervals(self._agent_context_intervals())

    @staticmethod
    def _xor_agent_context_intervals(
        base_intervals: List[Tuple[int, int]], start: int, end: int
    ) -> List[Tuple[int, int]]:
        from ui.views.text_selection import xor_intervals
        return xor_intervals(base_intervals, start, end)

    def _toggle_agent_context_interval(self, start: int, end: int):
        self._set_agent_context_intervals(
            self._xor_agent_context_intervals(
                self._agent_context_intervals(), start, end
            )
        )

    @staticmethod
    def _is_keyboard_selection_navigation(event: QKeyEvent) -> bool:
        if not (event.modifiers() & Qt.ShiftModifier):
            return False
        return event.key() in {
            Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down,
            Qt.Key_Home, Qt.Key_End, Qt.Key_PageUp, Qt.Key_PageDown,
        }

    def _apply_keyboard_agent_selection(self):
        if not self._agent_context_active or self._agent_keyboard_context_base is None:
            return
        cursor = self.textCursor()
        if not cursor.hasSelection():
            self._set_agent_context_intervals(self._agent_keyboard_context_base)
            return
        self._set_agent_context_intervals(
            self._xor_agent_context_intervals(
                self._agent_keyboard_context_base,
                cursor.selectionStart(),
                cursor.selectionEnd(),
            )
        )

    def _set_global_indent_mode(self, use_tabs: bool):
        self.use_tabs = bool(use_tabs)

    def _reset_wrapped_horizontal_offset(self):
        if self.lineWrapMode() != QPlainTextEdit.NoWrap:
            bar = self.horizontalScrollBar()
            if bar.value() != 0:
                bar.setValue(0)

    def scrollContentsBy(self, dx: int, dy: int):
        # Dynamic wrapping must never pan horizontally.  Qt can briefly request
        # horizontal scrolling while keeping a just-typed cursor visible; drop
        # that component and let the wrapped layout move the cursor vertically.
        if self.lineWrapMode() != QPlainTextEdit.NoWrap and dx:
            super().scrollContentsBy(0, dy)
            self._schedule_horizontal_reset()
            return
        super().scrollContentsBy(dx, dy)

    def wrap_width_pixels(self) -> float:
        """Return the exact width used by the editor for wrapped text."""
        doc = self.document()
        return max(1.0, float(self.viewport().width()) - 2.0 * float(doc.documentMargin()))

    def _sync_wrap_layout(self):
        """Keep Qt's wrap width tied to the real, already-margined viewport.

        QPlainTextEdit supports only ``NoWrap`` and ``WidgetWidth``.  The
        viewport margins define the real text area, so wrapping must follow the
        viewport itself.  This helper only forces a relayout and clears any
        stale horizontal offset after geometry changes.
        """
        self._wrap_sync_timer.stop()
        if self.lineWrapMode() == QPlainTextEdit.NoWrap:
            self.viewport().update()
            self.minimap.update()
            return

        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self._reset_wrapped_horizontal_offset()
        key = (self.viewport().width(), self.font().toString(), self.isVisible(),
               self.document().documentMargin())
        if key == self._wrap_layout_key:
            return
        self._wrap_layout_key = key
        try:
            self.document().markContentsDirty(0, max(1, self.document().characterCount()))
        except Exception:
            pass
        self.viewport().update()
        self.minimap.update()
        # Qt may adjust scroll bars once more when the dirty document is laid
        # out.  Reset the invisible horizontal bar after that pass as well.
        self._schedule_horizontal_reset()

    def _force_visible_wrap_relayout(self):
        """Rebuild wrapped layout after a hidden/restored tab becomes visible.

        A document can be loaded while its editor host still has a zero or
        stale width (notably during session restore).  ``WidgetWidth`` then
        caches line layout against that old geometry.  Re-toggling the same
        wrap mode after the real viewport exists forces Qt to rebuild every
        visual line against the current viewport, without changing the user's
        wrap setting.
        """
        if self.lineWrapMode() == QPlainTextEdit.NoWrap:
            self.viewport().update()
            self.minimap.update()
            return
        key = (self.viewport().width(), self.font().toString(), self.isVisible(),
               self.document().documentMargin())
        if key == self._forced_wrap_key:
            self._reset_wrapped_horizontal_offset()
            return
        self._forced_wrap_key = key
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.horizontalScrollBar().setValue(0)
        try:
            self.document().markContentsDirty(0, max(1, self.document().characterCount()))
        except Exception:
            pass
        self.viewport().update()
        self.minimap.update()
        self._schedule_horizontal_reset()

    def _smart_renumber_ordered_lists(self):
        self.markdown_handler.ordered_lists.renumber()

    def set_search_ranges(self, ranges: List[Tuple[int, int]], current: Optional[Tuple[int, int]] = None):
        self._decoration_revision += 1
        self._search_ranges = list(ranges or [])
        self._search_current_range = current
        self._update_decorations()

    def clear_search_ranges(self):
        self._decoration_revision += 1
        self._search_ranges = []
        self._search_current_range = None
        self._update_decorations()

    def _on_typing_idle(self):
        self.suppressed_line = None
        self._update_decorations()

    def focusOutEvent(self, event: QFocusEvent):
        self._typing_idle_timer.stop()
        self.suppressed_line = None
        self._update_decorations()
        super().focusOutEvent(event)

    def set_diagnostics(self, diagnostics: List[Dict[str, Any]]):
        diagnostics = diagnostics or []
        if diagnostics == self.diagnostics:
            return
        from .editor_diagnostics import DiagnosticIndex
        self._diagnostic_index = DiagnosticIndex(diagnostics)
        self._decoration_revision += 1
        self.diagnostics = diagnostics
        self._update_decorations()

    def _refresh_theme(self, *_):
        self._decorations_key = None
        self._diagnostic_selection_key = None
        self._update_decorations()

    def _update_decorations(self):
        if getattr(self, "_document_change_pending", False):
            return
        extra_selections: List[QTextEdit.ExtraSelection] = []
        doc = self.document()
        cursor_line = self.textCursor().blockNumber()
        first = max(0, self.firstVisibleBlock().blockNumber() - 1)
        last = min(doc.blockCount() - 1, self.cursorForPosition(self.viewport().rect().bottomRight()).blockNumber() + 1)
        caret_rect = self.cursorRect()
        key = (self._content_revision, self._decoration_revision, cursor_line, caret_rect.y(), caret_rect.height(), first, last,
               self.suppressed_line, self.isReadOnly(), self._heading_flash_block,
               tuple(self._agent_context_intervals()))
        if key == self._decorations_key:
            return
        self._decorations_key = key

        from .editor_diagnostics import DiagnosticStatuses
        self.line_status_map = DiagnosticStatuses(self._diagnostic_index, self.suppressed_line)
        if not self.isReadOnly() and self.line_status_map.get(cursor_line) is None:
            line_sel = QTextEdit.ExtraSelection()
            line_sel.format.setBackground(theme_manager.color('background.surface'))
            line_sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            line_sel.cursor = self.textCursor()
            line_sel.cursor.clearSelection()
            extra_selections.append(line_sel)

        diagnostic_key = (self._content_revision, self._decoration_revision,
                          self.suppressed_line, first, last)
        if diagnostic_key != self._diagnostic_selection_key:
            self._diagnostic_selections = self._diagnostic_index.selections(
                self, first, last, self.suppressed_line, self.line_status_map)
            self._diagnostic_selection_key = diagnostic_key
        extra_selections.extend(self._diagnostic_selections)

        if self._heading_flash_block is not None:
            block = doc.findBlockByNumber(self._heading_flash_block)
            if block.isValid():
                flash = QTextEdit.ExtraSelection()
                flash.cursor = QTextCursor(block)
                flash.cursor.clearSelection()
                flash.format.setProperty(QTextFormat.FullWidthSelection, True)
                flash.format.setBackground(QColor(56, 189, 248, 72))
                extra_selections.append(flash)

        for agent_cursor in self._agent_context_cursors:
            if not agent_cursor.hasSelection():
                continue
            agent_sel = QTextEdit.ExtraSelection()
            agent_sel.cursor = QTextCursor(agent_cursor)
            agent_sel.format.setBackground(QColor(99, 102, 241, 82))
            agent_sel.format.setUnderlineStyle(QTextCharFormat.SingleUnderline)
            agent_sel.format.setUnderlineColor(QColor(129, 140, 248, 190))
            extra_selections.append(agent_sel)

        for start, end in self._search_ranges:
            start = max(0, min(start, doc.characterCount() - 1))
            end = max(0, min(end, doc.characterCount() - 1))
            if end <= start:
                continue
            search_sel = QTextEdit.ExtraSelection()
            c = QTextCursor(doc)
            c.setPosition(start)
            c.setPosition(end, QTextCursor.KeepAnchor)
            search_sel.cursor = c
            if self._search_current_range == (start, end):
                search_sel.format.setBackground(QColor(251, 191, 36, 125))
                search_sel.format.setUnderlineStyle(QTextCharFormat.SingleUnderline)
                search_sel.format.setUnderlineColor(QColor(251, 191, 36, 220))
            else:
                search_sel.format.setBackground(QColor(96, 165, 250, 55))
            extra_selections.append(search_sel)

        self.setExtraSelections(extra_selections)
        self.viewport().update()
        self.line_number_area.update()
        self.minimap.update()
        if hasattr(self, "outline_rail"):
            self.outline_rail.update()

    def navigate_to_heading(self, block_number: int):
        block = self.document().findBlockByNumber(int(block_number))
        if not block.isValid():
            return
        self._heading_flash_block = block.blockNumber()
        self._heading_flash_timer.start()

        cursor = QTextCursor(block)
        cursor.movePosition(QTextCursor.StartOfBlock)
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()
        self._update_decorations()

    def _clear_heading_flash(self):
        if self._heading_flash_block is None:
            return
        self._heading_flash_block = None
        self._update_decorations()

    def _handle_code_enter(self) -> bool:
        cursor = self.textCursor()
        block = cursor.block()
        line = block.text()
        pos = cursor.positionInBlock()
        line_prefix = line[:pos]

        indent_match = re.match(r"^(\s*)", line_prefix)
        indent = indent_match.group(1) if indent_match else ""
        indent_step = "\t" if self.use_tabs else "    "
        extra = indent_step if line_prefix.rstrip().endswith(":") else ""

        cursor.beginEditBlock()
        cursor.insertText(f"\n{indent}{extra}")
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.ensureCursorVisible()
        return True

    def keyPressEvent(self, event: QKeyEvent):
        from ui.views.text_selection import wrap_selected_text
        if wrap_selected_text(self, event):
            return
        keyboard_agent_selection = (
            self._agent_context_active
            and self._is_keyboard_selection_navigation(event)
        )
        select_all_for_agent = (
            self._agent_context_active
            and event.matches(QKeySequence.SelectAll)
        )

        if keyboard_agent_selection and self._agent_keyboard_context_base is None:
            self._agent_keyboard_context_base = self._agent_context_intervals()

        # Ctrl+A is a complete selection gesture on its own rather than a
        # Shift-held sequence, so each press uses a fresh context snapshot.
        select_all_base = self._agent_context_intervals() if select_all_for_agent else None

        # 1. Shift+Enter создает честную новую строку
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if event.modifiers() & Qt.ShiftModifier:
                if self.is_markdown and self.markdown_handler.handle_key_press(event):
                    event.accept()
                    return
                cursor = self.textCursor()
                cursor.beginEditBlock()
                cursor.insertBlock()
                cursor.endEditBlock()
                self.setTextCursor(cursor)
                self.ensureCursorVisible()
                event.accept()
                return

        # List levels use a real tab, independent of indentation chosen for code.
        if (self.is_markdown and event.key() in (Qt.Key_Tab, Qt.Key_Backtab)
                and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier))
                and self.markdown_handler.handle_key_press(event)):
            event.accept()
            return

        # Global indentation mode applies to every editable file type.
        if event.key() == Qt.Key_Tab and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
            cursor = self.textCursor()
            indent_str = "\t" if self.use_tabs else "    "
            cursor.beginEditBlock()
            if cursor.hasSelection():
                start_b, end_b = cursor.selectionStart(), cursor.selectionEnd()
                cursor.setPosition(start_b); s_idx = cursor.blockNumber()
                cursor.setPosition(end_b); e_idx = cursor.blockNumber()
                for b_num in range(s_idx, e_idx + 1):
                    block = self.document().findBlockByNumber(b_num)
                    c = QTextCursor(block); c.movePosition(QTextCursor.StartOfBlock); c.insertText(indent_str)
            else:
                cursor.insertText(indent_str)
                self.setTextCursor(cursor)
            cursor.endEditBlock()
            # Qt lays a literal tab out against the native four-space tab-stop
            # grid.  No per-block formatting is injected here: that formatting
            # was the source of alternating/stale tab widths while typing.
            self.viewport().update()
            event.accept(); return
        if event.key() == Qt.Key_Backtab:
            cursor = self.textCursor(); cursor.beginEditBlock()
            first = self.document().findBlock(cursor.selectionStart())
            last = self.document().findBlock(max(cursor.selectionStart(), cursor.selectionEnd() - 1)) if cursor.hasSelection() else first
            block = first
            while block.isValid():
                text = block.text(); remove = 0
                if text.startswith("\t"): remove = 1
                elif text.startswith("    "): remove = 4
                elif text.startswith(" "): remove = min(4, len(text) - len(text.lstrip(" ")))
                if remove:
                    c = QTextCursor(block); c.movePosition(QTextCursor.StartOfBlock)
                    for _ in range(remove): c.deleteChar()
                if block.blockNumber() >= last.blockNumber(): break
                block = block.next()
            cursor.endEditBlock(); event.accept(); return

        # 2. Обработка Markdown
        if self.is_markdown:
            if self.markdown_handler.handle_key_press(event):
                event.accept()
                return

        # 3. Обычный код
        else:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                if not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
                    if self._handle_code_enter():
                        event.accept()
                        return


        from ui.text_navigation import move_at_text_boundary
        if not move_at_text_boundary(self, event):
            super().keyPressEvent(event)

        if keyboard_agent_selection:
            self._apply_keyboard_agent_selection()
        elif select_all_for_agent and select_all_base is not None:
            cursor = self.textCursor()
            if cursor.hasSelection():
                self._set_agent_context_intervals(
                    self._xor_agent_context_intervals(
                        select_all_base,
                        cursor.selectionStart(),
                        cursor.selectionEnd(),
                    )
                )

    def keyReleaseEvent(self, event: QKeyEvent):
        # Releasing Shift commits the current keyboard-selection gesture.
        # The marked ranges already contain the final previewed result.
        if event.key() == Qt.Key_Shift:
            self._agent_keyboard_context_base = None
        super().keyReleaseEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._agent_keyboard_context_base = None
            self._agent_left_mouse_down = True
        if self.is_markdown and self.markdown_handler.handle_mouse_press(event):
            self._agent_left_mouse_down = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        super().mouseReleaseEvent(event)
        if event.button() != Qt.LeftButton:
            return

        self._agent_left_mouse_down = False
        cursor = self.textCursor()
        if self._agent_context_active:
            if cursor.hasSelection():
                self._toggle_agent_context_interval(cursor.selectionStart(), cursor.selectionEnd())
            return

    def mouseMoveEvent(self, event: QMouseEvent):
        global_pos = event.globalPosition().toPoint()

        if self.is_markdown and (event.modifiers() & Qt.ControlModifier):
            url = self.markdown_handler.get_url_at_pos(global_pos)
            self.viewport().setCursor(Qt.PointingHandCursor if url else Qt.IBeamCursor)
        else:
            self.viewport().setCursor(Qt.IBeamCursor)

        diag_msg = self._get_diagnostic_at_pos(global_pos)
        if diag_msg:
            QToolTip.showText(global_pos, diag_msg, self.viewport())
        else:
            QToolTip.hideText()

        super().mouseMoveEvent(event)

    def _diagnostic_message_for_line(self, line: int) -> Optional[str]:
        messages = []
        for diag in self._diagnostic_index.intersecting(line, line):
            if self.suppressed_line is not None and line == self.suppressed_line:
                continue
            rng = diag.get("range", {})
            if rng.get("start", {}).get("line", 0) != line:
                continue
            source = diag.get("source", "LSP")
            msg = diag.get("message", "").strip()
            tag = tr('ui.caption.1bcf13cd76f0') if diag.get("severity", 1) == 1 else tr('ui.caption.dd952698567c')
            messages.append(f"[{source} {tag}] {msg}")
        return "\n".join(messages) if messages else None

    def _get_diagnostic_at_pos(self, global_pos: QPoint) -> Optional[str]:
        viewport_pos = self.viewport().mapFromGlobal(global_pos)
        cursor = self.cursorForPosition(viewport_pos)
        line = cursor.blockNumber()
        col = cursor.positionInBlock()
        messages = []

        for diag in self._diagnostic_index.intersecting(line, line):
            if self.suppressed_line is not None and line == self.suppressed_line:
                continue

            rng = diag.get("range", {})
            st_line = rng.get("start", {}).get("line", 0)
            st_col = rng.get("start", {}).get("character", 0)
            en_line = rng.get("end", {}).get("line", st_line)
            en_col = rng.get("end", {}).get("character", st_col)

            if not (st_line <= line <= en_line):
                continue
            if line == st_line and col < st_col:
                continue
            if line == en_line and col > en_col:
                continue
            source = diag.get("source", "LSP")
            msg = diag.get("message", "").strip()
            tag = tr('ui.caption.1bcf13cd76f0') if diag.get("severity", 1) == 1 else tr('ui.caption.dd952698567c')
            messages.append(f"[{source} {tag}] {msg}")

        return "\n".join(messages) if messages else None

    def _draw_error_icon(self, painter: QPainter, x: float, y: float, size: float):
        cx = x + size / 2.0
        cy = y + size / 2.0
        painter.setBrush(QBrush(QColor("#ef4444")))
        painter.setPen(Qt.NoPen)
        painter.drawEllipse(QRectF(x, y, size, size))

        painter.setPen(QPen(theme_manager.color('text.primary'), 1.6, Qt.SolidLine, Qt.RoundCap))
        d = size * 0.26
        painter.drawLine(QPointF(cx - d, cy - d), QPointF(cx + d, cy + d))
        painter.drawLine(QPointF(cx + d, cy - d), QPointF(cx - d, cy + d))

    def _draw_warning_icon(self, painter: QPainter, x: float, y: float, size: float):
        path = QPainterPath()
        path.moveTo(x + size / 2.0, y + 1.0)
        path.lineTo(x + size, y + size - 1.0)
        path.lineTo(x, y + size - 1.0)
        path.closeSubpath()

        painter.setBrush(QBrush(QColor("#eab308")))
        painter.setPen(QPen(QColor("#ca8a04"), 0.8))
        painter.drawPath(path)

        cx = x + size / 2.0
        painter.setPen(QPen(theme_manager.color('background.panel'), 1.8, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(cx, y + size * 0.38), QPointF(cx, y + size * 0.65))
        painter.drawPoint(QPointF(cx, y + size * 0.82))

    def paintEvent(self, event):
        if self.is_markdown:
            p = QPainter()
            if p.begin(self.viewport()):
                try:
                    p.setRenderHint(QPainter.Antialiasing)
                    self.markdown_handler.paint_markdown_elements(p)
                finally:
                    p.end()

        from .text_alignment import apply_visible
        apply_visible(self)
        super().paintEvent(event)

        # Отрисовка табуляций. Coordinates are derived from QTextCursor
        # geometry rather than fixed character advances, so a tab in the
        # middle of proportional text is drawn exactly where Qt lays it out.
        p_tab = QPainter(self.viewport())
        from .text_alignment import paint_selection_gutters
        paint_selection_gutters(self, p_tab)
        p_tab.setRenderHint(QPainter.Antialiasing, True)
        if self.is_markdown:
            self.markdown_handler.paint_markdown_markers(p_tab)

        pen_chevron = QPen(themed_color('text.secondary', 85), 1.2, Qt.SolidLine, Qt.RoundCap)
        pen_dot = QPen(themed_color('text.primary', 150), 2.0, Qt.SolidLine, Qt.RoundCap)

        space_w = self.fontMetrics().horizontalAdvance(' ')
        tab_w = space_w * 4
        if abs(self.tabStopDistance() - tab_w) > 0.5:
            self.setTabStopDistance(tab_w)

        selection = self.textCursor()
        has_sel = selection.hasSelection()
        sel_start = selection.selectionStart() if has_sel else -1
        sel_end = selection.selectionEnd() if has_sel else -1

        block = self.firstVisibleBlock()
        while block.isValid():
            block_geom = self.blockBoundingGeometry(block).translated(self.contentOffset())
            if block_geom.top() > self.viewport().height():
                break
            if block_geom.bottom() >= 0 and block.isVisible():
                text = block.text()
                block_pos = block.position()
                in_leading = True
                i = 0
                while i < len(text):
                    ch = text[i]
                    if ch == '\t':
                        if self.is_markdown:
                            i += 1
                            continue
                        before = QTextCursor(self.document())
                        before.setPosition(block_pos + i)
                        after = QTextCursor(self.document())
                        after.setPosition(block_pos + i + 1)
                        rb = self.cursorRect(before)
                        ra = self.cursorRect(after)
                        # Same visual line is the normal case. If Qt wraps at
                        # the tab boundary, fall back to one tab stop width.
                        if abs(ra.center().y() - rb.center().y()) <= max(2, rb.height()):
                            x1, x2 = float(rb.x()), float(ra.x())
                            if x2 <= x1:
                                x2 = x1 + tab_w
                        else:
                            x1, x2 = float(rb.x()), float(rb.x()) + tab_w
                        cy = float(rb.center().y())
                        # The marker is a fixed-size glyph clipped to the
                        # whitespace that Qt actually allocated to this tab.
                        # It therefore cannot extend into or be covered by the
                        # following text even when the next tab stop is close.
                        cell_left = x1 + 1.0
                        cell_width = max(0.0, x2 - x1 - 2.0)
                        if cell_width >= 1.0:
                            p_tab.save()
                            p_tab.setClipRect(QRectF(cell_left, float(rb.y()), cell_width, float(rb.height())))
                            p_tab.setPen(pen_chevron)
                            # Visual-only tab marker: the compact chevron used
                            # by the editor originally.  Keep the current Qt
                            # tab-stop geometry untouched.
                            cx = cell_left + cell_width / 2.0
                            p_tab.drawLine(QPointF(cx - 2.0, cy - 3.0), QPointF(cx + 1.5, cy))
                            p_tab.drawLine(QPointF(cx - 2.0, cy + 3.0), QPointF(cx + 1.5, cy))
                            p_tab.restore()
                        i += 1
                        continue

                    if in_leading and ch == ' ' and text[i:i + 4] == '    ':
                        if has_sel:
                            p_tab.setPen(pen_dot)
                            for offset in range(4):
                                abs_pos = block_pos + i + offset
                                if sel_start <= abs_pos < sel_end:
                                    c = QTextCursor(self.document())
                                    c.setPosition(abs_pos)
                                    r = self.cursorRect(c)
                                    p_tab.drawPoint(QPointF(float(r.x()) + space_w / 2.0, float(r.center().y())))
                        i += 4
                        continue

                    if ch != ' ':
                        in_leading = False
                    i += 1

            block = block.next()
        p_tab.end()


    def minimap_width(self) -> int:
        return self.minimap.WIDTH

    def line_number_area_width(self) -> int:
        # В Markdown нумерация скрыта (ширина 0)
        if getattr(self, "is_markdown", False):
            return 0
        # One shared rail for line numbers and diagnostic markers.
        return self.minimap_width()

    def left_rail_width(self) -> int:
        """Width of the editor chrome that lives to the left of the text.

        Code files use the line-number gutter. Markdown replaces that gutter
        with the outline rail, which intentionally has the same width as the
        minimap on the right.
        """
        if getattr(self, "is_markdown", False):
            return self.minimap_width()
        return self.line_number_area_width()

    def centered_text_geometry(self) -> tuple[int, int]:
        """Return ``(left, width)`` for the real text viewport.

        Horizontal centering is handled by ``CenteredEditorHost`` outside the
        QPlainTextEdit.  Inside the editor we reserve only actual chrome:
        gutter/outline on the left and minimap on the right.  This keeps
        QPlainTextEdit.WidgetWidth tied to a genuine viewport instead of a
        viewport narrowed by large synthetic centering margins.
        """
        left_rail = self.left_rail_width()
        reserve_left = left_rail + EDITOR_LINE_NUMBER_GAP if left_rail > 0 else 0
        reserve_right = self.minimap_width() + EDITOR_MINIMAP_GAP
        width = max(0, self.width() - reserve_left - reserve_right)
        return reserve_left, width

    def centered_content_margins(self) -> tuple[int, int]:
        left, _ = self.centered_text_geometry()
        right = self.minimap_width() + EDITOR_MINIMAP_GAP
        return left, right

    def centered_side_padding(self) -> int:
        # Backward-compatible convenience for callers that only need the left
        # side. New layout code should use centered_content_margins().
        return self.centered_content_margins()[0]

    def update_line_number_area_width(self, _=0):
        w = self.line_number_area_width()
        space_w = self.fontMetrics().horizontalAdvance(' ')
        self.setTabStopDistance(space_w * 4)

        # The reading column keeps its own centered geometry. Line numbers are
        # an external gutter beside that column, exactly like the minimap on
        # the right, so they no longer steal width from the text viewport.
        left_space, right_space = self.centered_content_margins()
        # QPlainTextEdit already contributes documentMargin() inside the
        # viewport. Markdown previously added another 18 px only on the left,
        # making the text visibly farther from the outline rail than from the
        # minimap. Keep Markdown symmetric: rail gap + document margin on both
        # sides. Code files retain their small gutter padding.
        # Keep the actual text start identical for Markdown and all other files.
        # Gutters are external chrome and must not shift the reading column.
        left_padding = 0
        self.setViewportMargins(
            left_space + left_padding,
            0,
            right_space,
            0,
        )

        if w > 0:
            gutter_x = 0
            self.line_number_area.setGeometry(QRect(gutter_x, 0, w, self.height()))
            self.line_number_area.show()
            self.line_number_area.raise_()
            self.outline_rail.hide()
        else:
            self.line_number_area.hide()
            if getattr(self, "is_markdown", False):
                outline_w = self.minimap_width()
                outline_x = max(0, left_space - EDITOR_LINE_NUMBER_GAP - outline_w)
                self.outline_rail.setGeometry(QRect(outline_x, 0, outline_w, self.height()))
                self.outline_rail.show()
                self.outline_rail.raise_()
            else:
                self.outline_rail.hide()

    def update_line_number_area(self, rect: QRect, dy: int):
        if hasattr(self, '_visible_decorations_timer'):
            self._visible_decorations_timer.start(0)
        if dy:
            self.line_number_area.scroll(0, dy)
        else:
            self.line_number_area.update(
                0, rect.y(), self.line_number_area.width(), rect.height()
            )

        if rect.contains(self.viewport().rect()):
            self.update_line_number_area_width(0)

        if self.is_markdown:
            self.markdown_handler.position_copy_buttons()

        if hasattr(self, "minimap"):
            self.minimap.update()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_line_number_area_width(0)
        text_left, text_width = self.centered_text_geometry()
        minimap_x = text_left + text_width + EDITOR_MINIMAP_GAP
        minimap_x = max(0, min(minimap_x, self.width() - self.minimap_width()))
        self.minimap.setGeometry(QRect(minimap_x, 0, self.minimap_width(), self.height()))
        if getattr(self, "is_markdown", False):
            outline_w = self.minimap_width()
            outline_x = max(0, text_left - EDITOR_LINE_NUMBER_GAP - outline_w)
            self.outline_rail.setGeometry(QRect(outline_x, 0, outline_w, self.height()))
            self.outline_rail.raise_()
            self.markdown_handler.position_copy_buttons()
        self._schedule_wrap_sync()

    def line_number_area_paint_event(self, event):
        painter = QPainter(self.line_number_area)
        painter.fillRect(event.rect(), theme_manager.color('background.window', shift=-0.02))

        w = self.line_number_area.width()

        # Четкая вертикальная граница между номерами строк и рабочей областью текста
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        # Keep the whole stroke inside the rail, including its left edge.
        painter.drawRect(QRectF(self.line_number_area.rect()).adjusted(0.5, 0.5, -0.5, -0.5))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = int(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + int(self.blockBoundingRect(block).height())

        current_block_num = self.textCursor().blockNumber()
        doc_margin = int(self.document().documentMargin())
        w = self.line_number_area.width()

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = str(block_number + 1)
                painter.setPen(theme_manager.color('text.primary') if block_number == current_block_num else theme_manager.color('text.disabled'))
                painter.setFont(self.font())

                block_height = bottom - top
                if not block.next().isValid():
                    block_height = max(self.fontMetrics().height(), block_height - doc_margin)

                # LSP markers share the same rail, with no second background.
                status = self.line_status_map.get(block_number)
                if status:
                    painter.save()
                    painter.setRenderHint(QPainter.Antialiasing)
                    icon_y = top + (block_height - 13.0) / 2.0
                    if status == 'error':
                        self._draw_error_icon(painter, 4.0, icon_y, 13.0)
                    else:
                        self._draw_warning_icon(painter, 4.0, icon_y, 13.0)
                    painter.restore()
                # Выравниваем номер строки строго по центру панели
                painter.drawText(
                    0,
                    top,
                    w,
                    block_height,
                    Qt.AlignCenter,
                    number
                )

            block = block.next()
            top = bottom
            bottom = top + int(self.blockBoundingRect(block).height())
            block_number += 1

    def toPlainText(self):
        return self.markdown_handler.source_text()

    def createMimeDataFromSelection(self):
        from ui.markdown_clipboard import selection_mime
        return selection_mime(self.markdown_handler, self.textCursor())

    def canInsertFromMimeData(self, mime):
        from ui.image_mime import can_import
        return (not self.isReadOnly() and self.markdown_handler.images.supported() and can_import(mime)) or super().canInsertFromMimeData(mime)

    def insertFromMimeData(self, mime):
        if not self.markdown_handler.images.insert_mime(mime):
            super().insertFromMimeData(mime)

    def setPlainText(self, text: str):
        self.markdown_handler.ordered_lists.reset()
        self._setting_plain_text = True
        try:
            super().setPlainText(text)
        finally:
            self._setting_plain_text = False
        self.update_line_number_area_width(0)
        self._schedule_wrap_sync()

# =========================================================================
# ВКЛАДКА ДОКУМЕНТА
# =========================================================================

class CenteredEditorHost(QWidget):
    """Centers the *text viewport* without synthetic viewport margins.

    The editor widget contains only its real chrome (left rail + minimap).
    This host positions that widget so the center of the text viewport, not
    the center of the chrome, stays aligned with the application center.
    """
    def __init__(self, editor: VSCodeEditor, parent=None):
        super().__init__(parent)
        self.setObjectName("CenteredEditorHost")
        self.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(self, "#CenteredEditorHost { background-color: @workspace.background@; }")
        self.editor = editor
        self.editor.setParent(self)
        self.editor.setMinimumWidth(0)
        self.editor.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def update_editor_geometry(self):
        if not self.editor:
            return
        left_rail = self.editor.left_rail_width()
        reserve_left = left_rail + EDITOR_LINE_NUMBER_GAP if left_rail > 0 else 0
        reserve_right = self.editor.minimap_width() + EDITOR_MINIMAP_GAP
        preferred = EDITOR_READING_WIDTH + reserve_left + reserve_right
        outer_w = max(0, min(self.width(), preferred))
        text_w = max(0, outer_w - reserve_left - reserve_right)

        # Aim the actual text viewport at the geometric center of the whole
        # application window. The host is already the free area between side
        # panels, so clamping here makes panels shift the editor only when they
        # physically leave too little room for the ideal global-center position.
        top_level = self.window()
        desired_center = self.mapFrom(
            top_level, QPoint(top_level.width() // 2, 0)
        ).x()
        x = int(round(desired_center - reserve_left - (text_w / 2.0)))
        x = max(0, min(x, max(0, self.width() - outer_w)))
        self.editor.setGeometry(x, 0, outer_w, self.height())
        self.editor.update_line_number_area_width(0)
        self.editor._schedule_wrap_sync()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_editor_geometry()


class FileDocumentTab(QWidget):
    state_changed = Signal()
    dirty_state_changed = Signal(bool)
    agent_request_submitted = Signal(str, object)

    def __init__(self, file_path: str, parent=None, *, memory_only=False, content='', allow_read_only_mode=True, allow_file_path=True):
        super().__init__(parent)
        self._memory_only = memory_only
        self._memory_content = content
        self._allow_read_only_mode = allow_read_only_mode
        self._read_only_view = None
        self._read_only_host = None
        self._svg_preview_active = False
        self._svg_host = None
        self.file_path = os.path.abspath(file_path)
        self.workspace_root = app_state.workspace
        self._load_ok = False
        self._load_error = None
        from core.text_files import TextFormat
        self._text_format = TextFormat()
        self.is_dirty = False
        self._notified_dirty = False
        self._dirty_notification_timer = QTimer(self)
        self._dirty_notification_timer.setSingleShot(True)
        self._dirty_notification_timer.setInterval(0)
        self._dirty_notification_timer.timeout.connect(self._emit_dirty_state)
        self.doc_version = 1
        self._disk_hash = None
        self._external_conflict = False
        self._model_modified = False
        self._origin_revision = 0
        self._acknowledged_disk_oid = None

        self._ast_diagnostics: List[Dict[str, Any]] = []
        self._lsp_diagnostics: List[Dict[str, Any]] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.toolbar = QFrame(self)
        self.toolbar.setObjectName("FileDocToolbar")
        self.toolbar.setFixedHeight(22)
        self.tb_layout = QHBoxLayout(self.toolbar)
        self.tb_layout.setContentsMargins(16, 2, 8, 2)
        self.tb_layout.setSpacing(6)

        rel_path = document_path(self.file_path)
        self.path_label = QLabel(rel_path, self.toolbar)
        self.path_label.setObjectName("FileDocBreadcrumb")
        self.path_label.setTextFormat(Qt.PlainText)
        self.tb_layout.addWidget(self.path_label)
        self.tb_layout.addStretch()

        layout.addWidget(self.toolbar)

        is_markdown_file = Path(self.file_path).suffix.lower() in {".md", ".markdown"}
        self.lsp_enabled = True
        self._lsp_activated = False
        self.editor_mode = "live" if is_markdown_file else "raw"
        self._last_editable_markdown_mode = self.editor_mode
        self.editor = VSCodeEditor(self)
        self.editor.set_file_path(self.file_path)
        self.editor.markdown_handler.set_visual_enabled(self.editor_mode == "live")

        self.agent_popup = AgentSelectionPopup(
            self.editor,
            lambda: self.file_path,
            parent=self.editor.viewport(),
            allow_file_path=allow_file_path and not memory_only,
        )
        self.editor.agent_popup_requested.connect(self._open_agent_popup)
        self.agent_popup.cancelled.connect(self.editor.cancel_agent_context)
        self.agent_popup.submitted.connect(self._submit_agent_popup)

        self.search_popup = SearchReplacePopup(self)
        self.search_popup.search_changed.connect(self._on_search_changed)
        self.search_popup.next_requested.connect(self._search_next)
        self.search_popup.replace_requested.connect(self._replace_current_search)
        self.search_popup.replace_all_requested.connect(self._replace_all_search)
        self.search_popup.closed.connect(self.editor.clear_search_ranges)
        self._search_matches: List[Tuple[int, int]] = []
        self._search_index = -1
        self.shortcut_find = QShortcut(QKeySequence.Find, self)
        self.shortcut_find.activated.connect(self.open_search_popup)

        self.content_stack = QStackedWidget(self)
        self.content_stack.setObjectName("FileDocumentContentStack")
        self.editor_host = CenteredEditorHost(self.editor, self.content_stack)
        self.content_stack.addWidget(self.editor_host)
        self.content_stack.setCurrentWidget(self.editor_host)
        layout.addWidget(self.content_stack, 1)
        from ui.views.selection_stats import SelectionStats
        self.selection_stats_host = QWidget(self)
        stats_layout = QVBoxLayout(self.selection_stats_host)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        self.selection_stats = SelectionStats(self.editor, lambda: self.file_path, self.selection_stats_host)
        stats_layout.addWidget(self.selection_stats)
        layout.addWidget(self.selection_stats_host)
        defer(0, self, self.editor_host.update_editor_geometry)

        self.highlighter = get_highlighter_for_file(self.file_path, self.editor.document(), self.editor)

        # The old permanent formatting/LSP toolbar was intentionally removed.
        # File-type-specific controls now live in a compact row above the normal
        # editor context menu and are created fresh on every right click.
        self.editor.setContextMenuPolicy(Qt.CustomContextMenu)
        self.editor.customContextMenuRequested.connect(
            lambda pos: self._show_editor_context_menu(self.editor, pos)
        )

        self.editor.document().modificationChanged.connect(self.set_dirty)

        self._lsp_debounce_timer = QTimer(self)
        self._lsp_debounce_timer.setSingleShot(True)
        self._lsp_debounce_timer.setInterval(300)
        self._lsp_debounce_timer.timeout.connect(self._sync_lsp_changes)
        self._preview_refresh_timer = QTimer(self)
        self._preview_refresh_timer.setSingleShot(True)
        self._preview_refresh_timer.timeout.connect(self._refresh_markdown_preview)

        self.editor.document().contentsChange.connect(
            lambda _pos, removed, added: self._on_editor_text_changed() if removed or added else None)
        self._update_centered_chrome()

        self.shortcut_save = QShortcut(QKeySequence("Ctrl+S"), self)
        self.shortcut_save.activated.connect(self.save_file)
        if Path(self.file_path).suffix.lower()=='.svg':
            self.shortcut_svg = QShortcut(QKeySequence('Ctrl+Shift+0'),self)
            self.shortcut_svg.setContext(Qt.WidgetWithChildrenShortcut)
            self.shortcut_svg.activated.connect(lambda:self.show_svg_image(not self._svg_preview_active))
            self.svg_mode_button = QToolButton(self.toolbar)
            set_text(self.svg_mode_button, 'ui.plugin_manager_pages.image_a029')
            self.svg_mode_button.setFixedHeight(18)
            self.svg_mode_button.setStyleSheet('padding:0px 6px;')
            self.svg_mode_button.clicked.connect(lambda:self.show_svg_image(not self._svg_preview_active))
            self.tb_layout.addWidget(self.svg_mode_button)

        self.file_watcher = QFileSystemWatcher(self)
        self.file_watcher.fileChanged.connect(self._schedule_external_check)
        self.file_watcher.directoryChanged.connect(self._schedule_external_check)
        self._external_check_timer = QTimer(self)
        self._external_check_timer.setSingleShot(True)
        self._external_check_timer.setInterval(75)
        self._external_check_timer.timeout.connect(self._check_external_file)
        from ui.core_bridge.async_runner import AsyncRunner
        self._origin_runner = AsyncRunner(self, max_threads=1)
        if not self._memory_only:
            from core.file_origins import watch, unwatch
            watch(self.file_path)
            self._origin_watch_path = [self.file_path]
            watched_path = self._origin_watch_path
            self.destroyed.connect(lambda: unwatch(watched_path[0]))

        lsp_manager.diagnostics_updated.connect(self._on_diagnostics_received)
        self.load_file_content()

    def refresh_editor_layout(self):
        """Refresh geometry/layout when this tab becomes the visible page."""
        if not hasattr(self, "editor_host"):
            return
        if self.editor_mode == 'read_only':
            self._read_only_view.minimap.update()
            return
        self.editor_host.update_editor_geometry()
        self.editor.update_line_number_area_width(0)
        if self.editor.lineWrapMode() != QPlainTextEdit.NoWrap:
            self.editor._force_visible_wrap_relayout()
        else:
            self.editor.viewport().update()
            self.editor.minimap.update()
        self.editor._decorations_key = None
        self.editor._update_decorations()

    def showEvent(self, event):
        super().showEvent(event)
        self.activate_lsp()
        # Session-restored/existing documents may have been laid out while the
        # stacked page was hidden.  Rebuild once the real width is available.
        self.refresh_editor_layout()
        defer(0, self, self.refresh_editor_layout)

    def activate_lsp(self):
        if self._memory_only:
            return
        if self._load_ok and not self._lsp_activated:
            self._lsp_activated = True
            if self.lsp_enabled:
                lsp_manager.notify_open(self.file_path, self.editor.toPlainText(), self.workspace_root)

    def _open_agent_popup(self, anchor: QPoint):
        self.agent_popup.open_at(anchor)

    def _submit_agent_popup(self, message: str, file_context: object):
        self.agent_popup.reset_and_hide()
        self.editor.cancel_agent_context()
        self.agent_request_submitted.emit(message, dict(file_context or {}))

    def cancel_agent_prompt(self):
        if self.agent_popup.isVisible() or self.editor._agent_context_active:
            self.agent_popup.reset_and_hide()
            self.editor.cancel_agent_context()

    def _context_tool_button(
        self,
        parent: QWidget,
        root_popup: QWidget,
        text: str,
        tooltip: str,
        callback,
        *,
        checkable: bool = False,
        checked: bool = False,
        enabled: bool = True,
    ) -> QToolButton:
        btn = QToolButton(parent)
        btn.setObjectName("FileEditorToolBtn")
        icon_name = {'Mode':'code','Markdown':'markdown','H':'text-size','B':'bold','I':'italic','S':'strikethrough',
                     '`':'code','Link':'link','>':'quote','List':'list','```':'code',
                     'Table':'table','—':'remove','Align':'align-left','Wrap':'text-wrap',
                     'LSP':'server-cog','Tab':'indent-increase','4 spaces':'indent-increase'}.get(str(text))
        if icon_name:
            set_icon(btn, icon_name, size=18, square=True)
        else:
            bind_caption(btn, text)
        bind_caption(btn, tooltip, setter='setToolTip')
        btn.setCursor(Qt.PointingHandCursor)
        btn.setCheckable(checkable)
        btn.setChecked(bool(checked))
        btn.setEnabled(bool(enabled))

        def invoke(_checked=False):
            if checkable:
                callback(bool(_checked))
            else:
                callback()
            # Toggle controls (Wrap/LSP/etc.) are settings, not one-shot
            # commands: keep the tools popup open while flipping them.
            if not checkable:
                root_popup.close()

        btn.clicked.connect(invoke)
        return btn

    def _context_menu_tool_button(
        self,
        parent: QWidget,
        root_popup: QWidget,
        text: str,
        tooltip: str,
        actions,
        *,
        enabled: bool = True,
    ) -> QToolButton:
        btn = QToolButton(parent)
        btn.setObjectName("FileEditorToolBtn")
        icon_name = {'Mode':'code','Markdown':'markdown','H':'text-size','B':'bold','I':'italic','S':'strikethrough',
                     '`':'code','Link':'link','>':'quote','List':'list','```':'code',
                     'Table':'table','—':'remove','Align':'align-left','Wrap':'text-wrap',
                     'LSP':'server-cog','Tab':'indent-increase','4 spaces':'indent-increase'}.get(str(text))
        if icon_name:
            set_icon(btn, icon_name, size=18, square=True)
        else:
            bind_caption(btn, text)
        bind_caption(btn, tooltip, setter='setToolTip')
        btn.setCursor(Qt.PointingHandCursor)
        btn.setEnabled(bool(enabled))
        btn.setPopupMode(QToolButton.InstantPopup)
        btn.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")

        popup = QMenu(btn)
        exclusive_group = None
        for item in actions:
            if len(item) == 3:
                label, callback, checked = item
                action = add_caption_action(popup, 'addAction', label)
                action.setCheckable(True)
                action.setChecked(bool(checked))
                if exclusive_group is None:
                    from PySide6.QtGui import QActionGroup
                    exclusive_group = QActionGroup(popup)
                    exclusive_group.setExclusive(True)
                add_caption_action(exclusive_group, 'addAction', action)
            else:
                label, callback = item
                action = add_caption_action(popup, 'addAction', label)

            def invoke(_checked=False, cb=callback):
                cb()
                root_popup.close()

            action.triggered.connect(invoke)

        btn.setMenu(popup)
        return btn

    def _build_context_tools_widget(self, root_popup: QWidget) -> Optional[QWidget]:
        ext = Path(self.file_path).suffix.lower()
        is_md = ext in {".md", ".markdown"}
        is_txt = ext == ".txt"

        panel = QFrame(root_popup)
        panel.setObjectName("FileContextToolsPanel")
        panel.setAttribute(Qt.WA_StyledBackground, True)
        apply_style(panel, """
            QFrame#FileContextToolsPanel {
                background-color: @background.panel@;
                border: 1px solid @border.subtle@;
                border-radius: 7px;
            }
            QToolButton#FileEditorToolBtn {
                min-width:26px;max-width:26px;min-height:26px;max-height:26px;padding:0;
            }
            """)

        buttons: List[QToolButton] = []
        if ext=='.svg':
            buttons.append(self._context_menu_tool_button(panel,root_popup,'Mode',tr('tools.c99a91ecdb'),[
                (tr('ui.caption.7a131f19291a'),lambda:self.show_svg_image(False),not self._svg_preview_active),
                (tr('ui.caption.96f5f7884011'),lambda:self.show_svg_image(True),self._svg_preview_active)]))

        if is_md:
            buttons.append(
                self._context_menu_tool_button(
                    panel, root_popup, "Markdown", tr('tools.e1e4aa19e9'),
                    [
                        (tr('tools.848123d14f'), lambda: self._set_markdown_mode("raw"), self.editor_mode == "raw"),
                        (tr('tools.b64ac05f17'), lambda: self._set_markdown_mode("live"), self.editor_mode == "live"),
                    ] + ([(tr('tools.8ac7673530'), lambda: self._set_markdown_mode("read_only"),
                           self.editor_mode == "read_only")] if self._allow_read_only_mode else []),
                )
            )

            edit_enabled = not self.editor.isReadOnly()
            buttons.append(
                self._context_menu_tool_button(
                    panel, root_popup, "H", tr('tools.57160078e1'),
                    [(f"H{level} — Heading {level}", lambda n=level: self._set_heading(n)) for level in range(1, 6)],
                    enabled=edit_enabled,
                )
            )
            buttons.append(self._context_tool_button(panel, root_popup, "B", tr('tools.94fee62e68'), lambda: self._wrap_selection("**", "**"), enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, "I", tr('tools.9bf37cb58a'), lambda: self._wrap_selection("*", "*"), enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, "S", tr('tools.9aabbcb2c1'), lambda: self._wrap_selection("~~", "~~"), enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, "`", tr('tools.e767deee63'), lambda: self._wrap_selection("`", "`"), enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, tr('tools.a6a32dbc56'), tr('tools.a6a32dbc56'), lambda: self._wrap_selection("[", "](url)"), enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, ">", tr('tools.cb19465f5f'), lambda: self._prefix_selected_lines("> "), enabled=edit_enabled))
            buttons.append(
                self._context_menu_tool_button(
                    panel, root_popup, "List", tr('tools.42fe90167b'),
                    [
                        (tr('tools.1e95bc1fb9'), lambda: self._prefix_selected_lines("- ")),
                        (tr('tools.2894e41b6a'), self._make_numbered_list),
                    ],
                    enabled=edit_enabled,
                )
            )
            buttons.append(self._context_tool_button(panel, root_popup, "```", tr('tools.7545380a29'), self._insert_code_block, enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, "Table", tr('tools.67318ec200'), self._insert_table, enabled=edit_enabled))
            buttons.append(self._context_tool_button(panel, root_popup, "—", tr('tools.868ced8bf6'), lambda: self._insert_block("\n---\n"), enabled=edit_enabled))

        if is_md:
            buttons.append(self._context_menu_tool_button(panel, root_popup, 'Align', tr('ui.caption.0d80e32bb39d'),
                [(label, lambda value=value:self._align_selected_lines(value))
                 for label,value in [(tr('tools.58eb9032e3'),'left'),(tr('tools.d946067427'),'center'),(tr('tools.883361d5d6'),'right')]],
                enabled=not self.editor.isReadOnly()))

        if is_md or is_txt:
            wrap_on = self.editor.lineWrapMode() != QPlainTextEdit.NoWrap
            buttons.append(
                self._context_tool_button(
                    panel, root_popup, "Wrap", tr('tools.ecd0f38a04'),
                    lambda checked: self._toggle_word_wrap(checked),
                    checkable=True, checked=wrap_on, enabled=True,
                )
            )

        buttons.append(
            self._context_menu_tool_button(
                panel, root_popup,
                "Tab" if EDITOR_INDENT_STATE.use_tabs else "4 spaces",
                tr('ui.caption.fcf7e8dbf329'),
                [
                    (tr('ui.caption.de030da96932'), lambda: EDITOR_INDENT_STATE.set_use_tabs(False), not EDITOR_INDENT_STATE.use_tabs),
                    (tr('ui.caption.0bc8a45787d0'), lambda: EDITOR_INDENT_STATE.set_use_tabs(True), EDITOR_INDENT_STATE.use_tabs),
                ],
            )
        )

        buttons.append(
            self._context_tool_button(
                panel, root_popup, "LSP", tr('tools.bc7245a153'),
                lambda checked: self._toggle_lsp(checked),
                checkable=True, checked=self.lsp_enabled,
            )
        )

        # The tool popup has a hard wrapping width. Buttons are packed from the
        # left edge and move to the next row instead of widening the panel.
        max_row_width = 244
        spacing = 4
        margins = 6
        outer = QVBoxLayout(panel)
        outer.setContentsMargins(margins, margins, margins, margins)
        outer.setSpacing(4)

        rows: List[Tuple[QHBoxLayout, int]] = []
        current_row = QHBoxLayout()
        current_row.setContentsMargins(0, 0, 0, 0)
        current_row.setSpacing(spacing)
        current_row.setAlignment(Qt.AlignLeft)
        current_width = 0
        current_count = 0
        outer.addLayout(current_row)
        rows.append((current_row, 0))

        for btn in buttons:
            btn.setFixedSize(28, 28)
            btn_width = 28
            projected = current_width + (spacing if current_count else 0) + btn_width
            if current_count and projected > max_row_width:
                current_row = QHBoxLayout()
                current_row.setContentsMargins(0, 0, 0, 0)
                current_row.setSpacing(spacing)
                current_row.setAlignment(Qt.AlignLeft)
                outer.addLayout(current_row)
                current_width = 0
                current_count = 0

            current_row.addWidget(btn)
            current_width += (spacing if current_count else 0) + btn_width
            current_count += 1

        panel.adjustSize()
        return panel

    def _build_context_agent_button(self, root_popup: QWidget, source: QWidget, pos: QPoint) -> Optional[QToolButton]:
        if source is not self.editor:
            return None

        button = QToolButton(root_popup)
        button.setObjectName("FileEditorAgentButton")
        set_icon(button, 'sparkles', square=True)
        set_text(button, 'ui.file_view.open_agent_request_f4a1', setter='setToolTip')
        button.setCursor(Qt.PointingHandCursor)
        button.setFixedSize(28, 28)
        apply_style(button, """
            QToolButton#FileEditorAgentButton {
                color: @text.primary@;
                background-color: @background.surface@;
                border: 1px solid @border.subtle@;
                border-radius: 4px;
                padding: 0;
            }
            QToolButton#FileEditorAgentButton:hover {
                background-color: @control.hover@;
                border-color: @text.disabled@;
            }
            """)

        anchor = QPoint(pos)

        def open_agent():
            root_popup.close()
            defer(
                0, self,
                lambda a=QPoint(anchor): self.editor.activate_agent_context(
                    a,
                    include_current_selection=True,
                ),
            )

        button.clicked.connect(open_agent)
        return button

    def _show_editor_context_menu(self, source: QWidget, pos: QPoint):
        # One Qt popup contains two visually separate surfaces: a compact tools
        # panel on top and the editor's native actions below it. This keeps the
        # toolbar interactive while preserving normal outside-click dismissal.
        popup = QFrame(None, Qt.Popup | Qt.FramelessWindowHint)
        popup.setObjectName("FileEditorContextPopup")
        popup.setAttribute(Qt.WA_TranslucentBackground, True)
        apply_style(popup, """
            QFrame#FileEditorContextPopup { background: transparent; border: none; }
            QFrame#FileEditorStandardMenu {
                background-color: @background.panel@;
                border: 1px solid @border.subtle@;
                border-radius: 7px;
            }
            QFrame#FileEditorStandardActionRow {
                background: transparent;
                border: none;
            }
            QToolButton#FileEditorStandardAction {
                background: transparent;
                border: none;
                border-radius: 4px;
                color: @text.primary@;
                min-height: 25px;
                padding: 2px 8px;
                text-align: left;
            }
            QToolButton#FileEditorStandardAction:hover { background-color: @control.hover@; color: @text.primary@; }
            QToolButton#FileEditorStandardAction:disabled { color: @text.disabled@; }
            QLabel#FileEditorStandardShortcut {
                color: @text.secondary@;
                background: transparent;
                padding-right: 8px;
            }
            QLabel#FileEditorStandardShortcut:disabled { color: @text.disabled@; }
            """)

        popup_layout = QVBoxLayout(popup)
        popup_layout.setContentsMargins(0, 0, 0, 0)
        popup_layout.setSpacing(8)

        tools_widget = self._build_context_tools_widget(popup)
        if tools_widget is not None:
            tools_widget.adjustSize()
            tools_widget.setFixedWidth(256)
            popup_layout.addWidget(tools_widget, 0, Qt.AlignLeft)

        # Standalone agent affordance: formatting tools above, native context
        # menu below.  Unlike the selection affordance it is available even
        # when the editor currently has no selection.
        context_agent_button = self._build_context_agent_button(popup, source, pos)
        quick_row = QWidget(popup)
        quick_layout = QHBoxLayout(quick_row)
        quick_layout.setContentsMargins(0, 0, 0, 0)
        quick_layout.setSpacing(5)
        if context_agent_button is not None:
            quick_layout.addWidget(context_agent_button)
        search_btn = QToolButton(quick_row)
        search_btn.setObjectName("FileEditorAgentButton")
        set_icon(search_btn, 'search', square=True)
        set_text(search_btn, 'ui.canvas_view.search_and_replace_c227', setter='setToolTip')
        search_btn.setFixedSize(28, 28)
        apply_style(search_btn, "QToolButton {background:@background.surface@;border:1px solid @border.subtle@;border-radius:4px;padding:0;} QToolButton:hover {background:@control.hover@;}")
        search_btn.setCursor(Qt.PointingHandCursor)
        def open_search_from_context():
            global_anchor = source.mapToGlobal(pos)
            local_anchor = self.mapFromGlobal(global_anchor)
            popup.close()
            defer(0, self, lambda a=local_anchor: self.open_search_popup(a))
        search_btn.clicked.connect(open_search_from_context)
        quick_layout.addWidget(search_btn)
        quick_row.setFixedWidth(256)
        quick_layout.addStretch(1)
        popup_layout.addWidget(quick_row, 0, Qt.AlignLeft)

        standard_menu = source.createStandardContextMenu()
        if Path(self.file_path).suffix.lower() in {'.md','.markdown'} and not self._memory_only:
            from .markdown_anchors import link_target, copy_link
            line = self._markdown_context_line(source, pos)
            target = link_target(self.editor.toPlainText(),line) if line is not None else None
            standard_menu.addSeparator()
            copy_action = localize(add_caption_action(standard_menu, 'addAction', 'Копировать ссылку'), 'ui.file_view.copy_link_db19')
            copy_action.setEnabled(target is not None and
                (target[2] is None or not self.editor.isReadOnly() or self.editor_mode == 'read_only'))
            set_text(copy_action, 'ui.file_view.link_to_a_heading_or_paragraph_ab51', setter='setToolTip')
            copy_action.triggered.connect(lambda: copy_link(self.editor,self.file_path,line,
                allow_marker_in_preview=self.editor_mode=='read_only'))
        if not self._memory_only:
            standard_menu.addSeparator()
            history_action = localize(add_caption_action(standard_menu, 'addAction', 'История файла'), 'ui.file_view.file_history_a5e6')
            from ui.history_integration import open_history
            history_action.triggered.connect(lambda: open_history(self.window(), path=self.file_path))
        if source is self.editor and self.lsp_enabled:
            hit = self.editor.cursorForPosition(pos)
            line, column = hit.blockNumber(), hit.positionInBlock()
            diagnostic = next((d for d in self.editor.diagnostics
                               if d.get('range', {}).get('start', {}).get('line') == line
                               and d.get('range', {}).get('end', {}).get('line') == line
                               and d.get('range', {}).get('start', {}).get('character', 0) <= column
                               < d.get('range', {}).get('end', {}).get('character', 0)), None)
            item = lsp_manager.diagnostic_word(self.file_path, diagnostic) if diagnostic else None
            if item:
                server, word = item
                action = localize(add_caption_action(standard_menu, 'addAction', "Добавить в исключения"), 'ui.file_view.add_to_exceptions_93c9')
                bind_caption(action, word, setter='setToolTip')
                def add_exception():
                    try:
                        lsp_manager.add_exception(server, word)
                    except (OSError, ValueError) as error:
                        QMessageBox.warning(self, tr('ui.file_view.lsp_821e'), str(error))
                action.triggered.connect(add_exception)
        # Keep the native menu alive only as the owner/source of its QActions.
        # Do NOT reparent it into our popup: QMenu is a top-level popup widget and
        # would otherwise become visible at (0, 0), painting a duplicate Undo over
        # the tools surface.
        standard_menu.hide()
        popup._standard_menu = standard_menu

        menu_frame = QFrame(popup)
        menu_frame.setObjectName("FileEditorStandardMenu")
        menu_layout = QVBoxLayout(menu_frame)
        menu_layout.setContentsMargins(4, 4, 4, 4)
        menu_layout.setSpacing(0)

        has_visible_action = False
        for action in standard_menu.actions():
            if not action.isVisible():
                continue
            if action.isSeparator():
                separator = QFrame(menu_frame)
                separator.setFrameShape(QFrame.HLine)
                separator.setFixedHeight(9)
                apply_style(separator, "QFrame { color: @border.subtle@; background: transparent; margin: 4px 7px; }")
                menu_layout.addWidget(separator)
                continue

            # QTextEdit's standard menu commonly encodes the accelerator as
            # ``Action\tCtrl+X``. QToolButton does not lay that tab out like a
            # QMenu does, so split it explicitly into label + right column.
            raw_text = action.text() or ""
            if "\t" in raw_text:
                action_text, shortcut_text = raw_text.rsplit("\t", 1)
            else:
                action_text = raw_text
                shortcut = action.shortcut()
                shortcut_text = shortcut.toString(QKeySequence.NativeText) if not shortcut.isEmpty() else ""

            row = QFrame(menu_frame)
            row.setObjectName("FileEditorStandardActionRow")
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)

            button = QToolButton(row)
            button.setObjectName("FileEditorStandardAction")
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setAutoRaise(False)
            bind_caption(button, action_text)
            button.setIcon(action.icon())
            button.setEnabled(action.isEnabled())
            button.setCursor(Qt.PointingHandCursor)
            button.clicked.connect(action.trigger)
            action.triggered.connect(popup.close)
            row_layout.addWidget(button, 1)

            shortcut_label = QLabel(shortcut_text, row)
            shortcut_label.setObjectName("FileEditorStandardShortcut")
            shortcut_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            shortcut_label.setEnabled(action.isEnabled())
            shortcut_label.setMinimumWidth(62)
            row_layout.addWidget(shortcut_label, 0)

            menu_layout.addWidget(row)
            has_visible_action = True

        if not has_visible_action:
            empty = localize(QLabel('', menu_frame), 'ui.file_view.no_actions_219e')
            apply_style(empty, "color: @text.disabled@; padding: 6px 10px;")
            menu_layout.addWidget(empty)

        # Both surfaces follow the same workspace menu width.
        menu_frame.setFixedWidth(256)
        popup_layout.addWidget(menu_frame, 0, Qt.AlignLeft)

        popup.adjustSize()

        viewport = source.viewport() if hasattr(source, "viewport") else source
        click_pos = viewport.mapToGlobal(pos)

        # Keep the lower/native menu at the click position.  Everything added
        # before it (formatting panel + agent button + layout gaps) is therefore
        # positioned above the click point.
        popup_layout.activate()
        menu_offset = menu_frame.geometry().top()
        target = QPoint(click_pos.x(), click_pos.y() - menu_offset)
        screen = QApplication.screenAt(click_pos) or QApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry()
            target.setX(max(bounds.left(), min(target.x(), bounds.right() - popup.sizeHint().width() + 1)))
            target.setY(max(bounds.top(), min(target.y(), bounds.bottom() - popup.sizeHint().height() + 1)))

        # Keep a Python-side reference for the lifetime of the top-level popup.
        old_popup = getattr(self, "_editor_context_popup", None)
        if old_popup is not None:
            old_popup.close()
            old_popup.deleteLater()
        self._editor_context_popup = popup
        popup.destroyed.connect(lambda *_: setattr(self, "_editor_context_popup", None))

        popup.move(target)
        popup.show()
        popup.raise_()
        popup.setFocus(Qt.PopupFocusReason)

    def _markdown_context_line(self, source, pos):
        if source is self.editor:
            cursor = self.editor.textCursor()
            return (self.editor.document().findBlock(cursor.selectionStart()).blockNumber()
                    if cursor.hasSelection() else self.editor.cursorForPosition(pos).blockNumber())
        chunk = getattr(source, '_last_context_widget', None)
        if chunk is None or not hasattr(chunk, 'cursorForPosition'):
            return None
        global_pos = source.mapToGlobal(pos)
        cursor = chunk.cursorForPosition(chunk.viewport().mapFromGlobal(global_pos))
        block = cursor.block()
        fragment = block.begin()
        while not fragment.atEnd():
            for name in fragment.fragment().charFormat().anchorNames():
                match = re.fullmatch(r'toshy-heading-(\d+)',name)
                if match:
                    return int(match[1])
            fragment += 1
        return None

    def _update_centered_chrome(self):
        if hasattr(self, "tb_layout"):
            self.tb_layout.setContentsMargins(16, 2, 8, 2)
        if hasattr(self, "editor_host"):
            self.editor_host.update_editor_geometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_centered_chrome()

    def show_svg_image(self,enabled=True):
        if Path(self.file_path).suffix.lower()!='.svg':
            return
        if self._svg_host is None:
            from PySide6.QtSvgWidgets import QSvgWidget
            self._svg_host = QWidget(self.content_stack)
            layout = QVBoxLayout(self._svg_host)
            layout.setContentsMargins(12,8,12,8)
            self._svg_error = QLabel(self._svg_host)
            self._svg_error.setWordWrap(True)
            layout.addWidget(self._svg_error)
            self._svg_widget = QSvgWidget(self._svg_host)
            self._svg_widget.setMinimumSize(64,64)
            self._svg_widget.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Expanding)
            layout.addWidget(self._svg_widget,1)
            self.content_stack.addWidget(self._svg_host)
        if enabled:
            try:
                from PySide6.QtCore import QByteArray
                from .svg_preview import svg_bytes
                self._svg_widget.load(QByteArray(svg_bytes(self.editor.toPlainText())))
                if not self._svg_widget.renderer().isValid():
                    raise ValueError('Не удалось отрендерить SVG. Перейдите к исходнику, чтобы исправить его.')
                self._svg_widget.renderer().setAspectRatioMode(Qt.KeepAspectRatio)
                self._svg_error.hide()
                self._svg_widget.show()
            except ValueError as error:
                bind_caption(self._svg_error, str(error))
                self._svg_error.show()
                self._svg_widget.hide()
        self._svg_preview_active = bool(enabled)
        self.content_stack.setCurrentWidget(self._svg_host if enabled else self.editor_host)
        bind_caption(self.svg_mode_button, tr('ui.caption.0b5267f774e0') if enabled else tr('ui.plugin_manager_pages.image_a029'))
        self.selection_stats_host.setVisible(not enabled)
        if not enabled:
            self.refresh_editor_layout()
            self.editor.setFocus()

    def _sync_markdown_mode_ui(self):
        read_only = self.editor_mode == 'read_only'
        self.content_stack.setCurrentWidget(self._read_only_host if read_only else self.editor_host)
        self.selection_stats_host.setVisible(not read_only)
        if not read_only:
            self.selection_stats.schedule()

    def _ensure_read_only_view(self):
        if self._read_only_view is not None:
            return
        from .readonly_markdown import ReadOnlyMarkdownEditor
        self._read_only_host = QWidget(self.content_stack)
        layout = QHBoxLayout(self._read_only_host)
        layout.setContentsMargins(0, 0, 0, 0)
        self._read_only_view = ReadOnlyMarkdownEditor(self._read_only_host,
            context_menu_handler=self._show_editor_context_menu)
        self._read_only_view.setMaximumWidth(EDITOR_READING_WIDTH + 2 * CodeMinimap.WIDTH
                                             + EDITOR_LINE_NUMBER_GAP + EDITOR_MINIMAP_GAP)
        layout.addWidget(self._read_only_view, 1)
        self.content_stack.addWidget(self._read_only_host)

    def _align_selected_lines(self, value):
        if Path(self.file_path).suffix.lower() not in {'.md','.markdown'}:return
        from .text_alignment import set_selected
        set_selected(self.editor, value)

    def _refresh_markdown_preview(self):
        if self._svg_preview_active:
            self.show_svg_image(True)
        if self.editor_mode == 'read_only':
            from .text_alignment import mapping
            self._read_only_view.set_markdown(self.editor.toPlainText(), base_path=self.file_path,
                                               line_alignments=mapping(self.editor))

    def _set_markdown_mode(self, mode: str):
        if Path(self.file_path).suffix.lower() not in {".md", ".markdown"}:
            return
        if mode not in {"raw", "live", "read_only"}:
            return
        if mode == 'read_only':
            if not self._allow_read_only_mode:
                return
            if self.editor_mode != 'read_only':
                self._last_editable_markdown_mode = self.editor_mode
                line = self.editor.firstVisibleBlock().blockNumber()
                self.cancel_agent_prompt()
                self.search_popup.hide()
                self.editor_mode = mode
                self.editor.setReadOnly(True)
                self._ensure_read_only_view()
                self._refresh_markdown_preview()
                self._sync_markdown_mode_ui()
                self._read_only_view.minimap_scroll_to_row(max(0, line), center=False)
            self._read_only_view.renderer.setFocus()
            return
        line = (int(self._read_only_view.minimap_viewport_rows()[0])
                if self.editor_mode == 'read_only' else None)
        self._preview_refresh_timer.stop()
        self.editor_mode = mode
        self._last_editable_markdown_mode = mode
        self.editor.setReadOnly(False)
        live = mode == "live"
        self.editor.markdown_handler.set_enabled(True)
        self.editor.markdown_handler.set_visual_enabled(live)
        if live:
            if self.highlighter is None:
                self.highlighter = get_highlighter_for_file(self.file_path, self.editor.document(), self.editor)
            self.editor.markdown_handler.parse_code_blocks()
        else:
            if self.highlighter is not None:
                try:
                    self.highlighter.setDocument(None)
                except Exception:
                    pass
                self.highlighter.deleteLater()
                self.highlighter = None
            self.editor.viewport().update()
        self._sync_markdown_mode_ui()
        if line is not None:
            self.refresh_editor_layout()
            self.editor.minimap._visual_rows()
            row = self.editor.minimap._row_starts.get(line, 0)
            self.editor.minimap._scroll_to_document_row(row, center=False)
            self.editor.setFocus()

    def open_search_popup(self, pos: Optional[QPoint] = None):
        if self._svg_preview_active:
            self.show_svg_image(False)
        if self.editor_mode == 'read_only':
            self._read_only_view.open_search(pos)
            return
        self.search_popup.set_replace_enabled(not self.editor.isReadOnly() and not self._memory_only
                                              and getattr(self, '_search_replace_allowed', True))
        if pos is None:
            pos = QPoint(max(8, self.width() - 470), 34)
        self.search_popup.open_at(pos)

    @staticmethod
    def _find_ranges(text: str, query: str, case_sensitive: bool, whole_word: bool) -> List[Tuple[int, int]]:
        if not query:
            return []
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.escape(query)
        if whole_word:
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
        offsets = [0]
        for char in text:offsets.append(offsets[-1] + (2 if ord(char) > 0xffff else 1))
        return [(offsets[m.start()], offsets[m.end()]) for m in re.finditer(pattern, text, flags)]

    def _on_search_changed(self, query: str, case_sensitive: bool, whole_word: bool):
        self._search_matches = self._find_ranges(QPlainTextEdit.toPlainText(self.editor), query, case_sensitive, whole_word)
        self._search_index = 0 if self._search_matches else -1
        self._apply_search_state(center=False)

    def _apply_search_state(self, *, center: bool = True):
        current = self._search_matches[self._search_index] if 0 <= self._search_index < len(self._search_matches) else None
        self.editor.set_search_ranges(self._search_matches, current)
        self.search_popup.set_status(self._search_index, len(self._search_matches))
        if current is not None and center:
            c = QTextCursor(self.editor.document())
            c.setPosition(current[0]); c.setPosition(current[1], QTextCursor.KeepAnchor)
            self.editor.setTextCursor(c); self.editor.centerCursor(); self.editor.setFocus()

    def _search_next(self, backwards: bool = False):
        if not self._search_matches:
            return
        delta = -1 if backwards else 1
        self._search_index = (self._search_index + delta) % len(self._search_matches)
        self._apply_search_state()

    def _replace_current_search(self, replacement: str):
        if self.editor.isReadOnly() or self._memory_only or not getattr(self, '_search_replace_allowed', True) or not self._search_matches:
            return
        start, end = self._search_matches[self._search_index]
        c = QTextCursor(self.editor.document()); c.setPosition(start); c.setPosition(end, QTextCursor.KeepAnchor); c.insertText(replacement)
        self._on_search_changed(self.search_popup.find_edit.text(), self.search_popup.case_btn.isChecked(), self.search_popup.word_btn.isChecked())

    def _replace_all_search(self, replacement: str):
        if self.editor.isReadOnly() or self._memory_only or not getattr(self, '_search_replace_allowed', True) or not self._search_matches:
            return
        c = QTextCursor(self.editor.document()); c.beginEditBlock()
        try:
            for start, end in reversed(self._search_matches):
                c.setPosition(start); c.setPosition(end, QTextCursor.KeepAnchor); c.insertText(replacement)
        finally:
            c.endEditBlock()
        self._on_search_changed(self.search_popup.find_edit.text(), self.search_popup.case_btn.isChecked(), self.search_popup.word_btn.isChecked())

    def _toggle_word_wrap(self, checked: bool):
        if checked:
            self.editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
            self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.editor.horizontalScrollBar().setValue(0)
        else:
            self.editor.setLineWrapMode(QPlainTextEdit.NoWrap)
            self.editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.editor._schedule_wrap_sync()

    def _toggle_lsp(self, checked: bool):
        self.lsp_enabled = bool(checked)
        self._lsp_debounce_timer.stop()
        if self.lsp_enabled and self._lsp_activated:
            root_dir = self.workspace_root
            lsp_manager.notify_open(self.file_path, self.editor.toPlainText(), root_dir)
            self._sync_lsp_changes()
        else:
            lsp_manager.notify_close(self.file_path)
            self._lsp_diagnostics.clear()
            self._refresh_combined_diagnostics()

    def _wrap_selection(self, prefix: str, suffix: str):
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        if cursor.hasSelection():
            selected = self.editor.markdown_handler.selected_source_text(cursor)
            cursor.insertText(f"{prefix}{selected}{suffix}")
        else:
            start = cursor.position()
            cursor.insertText(prefix + suffix)
            cursor.setPosition(start + len(prefix))
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _selected_block_range(self):
        cursor = self.editor.textCursor()
        start = cursor.selectionStart()
        end = cursor.selectionEnd()
        doc = self.editor.document()
        first = doc.findBlock(start)
        last = doc.findBlock(max(start, end - 1)) if end > start else first
        return first, last

    def _prefix_selected_lines(self, prefix: str):
        first, last = self._selected_block_range()
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        block = first
        while block.isValid():
            cursor.setPosition(block.position())
            cursor.insertText(prefix)
            if block.blockNumber() >= last.blockNumber():
                break
            block = block.next()
        cursor.endEditBlock()
        self.editor.setFocus()

    def _set_heading(self, level: int):
        level = max(1, min(5, int(level)))
        first, last = self._selected_block_range()
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        block = first
        prefix = "#" * level + " "
        while block.isValid():
            text = block.text()
            cleaned = re.sub(r"^#{1,6}\s+", "", text)
            cursor.setPosition(block.position())
            cursor.setPosition(block.position() + len(text), QTextCursor.KeepAnchor)
            cursor.insertText(prefix + cleaned)
            if block.blockNumber() >= last.blockNumber():
                break
            block = cursor.block().next()
        cursor.endEditBlock()
        self.editor.setFocus()

    def _make_numbered_list(self):
        first, last = self._selected_block_range()
        cursor = QTextCursor(self.editor.document())
        cursor.beginEditBlock()
        block = first
        number = 1
        while block.isValid():
            cursor.setPosition(block.position())
            cursor.insertText(f"{number}. ")
            number += 1
            if block.blockNumber() >= last.blockNumber():
                break
            block = block.next()
        cursor.endEditBlock()
        self.editor.setFocus()

    def _insert_block(self, text: str):
        cursor = self.editor.textCursor()
        cursor.insertText(text)
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _insert_code_block(self):
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            selected = self.editor.markdown_handler.selected_source_text(cursor)
            cursor.insertText(f"```\n{selected}\n```")
        else:
            pos = cursor.position()
            cursor.insertText("```\n\n```")
            cursor.setPosition(pos + 4)
            self.editor.setTextCursor(cursor)
        self.editor.setFocus()

    def _insert_table(self):
        self._insert_block("| Column 1 | Column 2 |\n| --- | --- |\n|  |  |")

    def _on_editor_text_changed(self):
        # Debounce also drives the local Python syntax check; only the external
        # language-server notification itself is gated by lsp_enabled.
        self._lsp_debounce_timer.start()
        if self.editor_mode == 'read_only':
            self._preview_refresh_timer.start()

    def _sync_lsp_changes(self):
        text = self.editor.toPlainText()
        self.doc_version += 1

        local_errors = []
        if self.file_path.endswith(".py"):
            try:
                compile(text, self.file_path, 'exec')
            except SyntaxError as e:
                line = max(0, (e.lineno or 1) - 1)
                col = max(0, (e.offset or 1) - 1)
                end_col = col + 1
                if e.text:
                    end_col = max(end_col, len(e.text.rstrip('\r\n')))
                local_errors.append({
                    "range": {
                        "start": {"line": line, "character": col},
                        "end": {"line": line, "character": end_col}
                    },
                    "severity": 1,
                    "message": f"SyntaxError: {e.msg}",
                    "source": "Python AST"
                })

        self._ast_diagnostics = local_errors
        self._refresh_combined_diagnostics()

        if self.lsp_enabled and not self._memory_only:
            lsp_manager.notify_change(self.file_path, text, self.doc_version)

    def _on_diagnostics_received(self, file_path: str, diagnostics: list):
        if not self.lsp_enabled:
            return
        if os.path.normpath(file_path).lower() == os.path.normpath(self.file_path).lower():
            self._lsp_diagnostics = diagnostics or []
            self._refresh_combined_diagnostics()

    def _refresh_combined_diagnostics(self):
        combined = list(self._ast_diagnostics)
        ast_lines = {d.get("range", {}).get("start", {}).get("line") for d in self._ast_diagnostics}

        doc = self.editor.document()
        is_md = getattr(self.editor, "is_markdown", False)

        for d in self._lsp_diagnostics:
            # 1. Игнорируем предупреждения о длине строк
            if "e501" in str(d.get("code", "")).lower() or "line too long" in d.get("message", "").lower():
                continue

            st_line = d.get("range", {}).get("start", {}).get("line", 0)
            if st_line in ast_lines and "syntax" in d.get("message", "").lower():
                continue

            # 2. Исключения разметки для Markdown-файлов
            if is_md:
                block = doc.findBlockByNumber(st_line)
                if block.isValid():
                    line_text = block.text()
                    stripped = line_text.strip()

                    # Разделители таблиц (|---|---|) и горизонтальные линии
                    if re.match(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$", line_text):
                        continue
                    if re.match(r"^(---|___|\*\*\*)$", stripped):
                        continue

                    # Блоки кода ```
                    if block.userState() == 1 or stripped.startswith("```"):
                        continue

                    # Игнорируем, если ошибка указывает на служебные символы разметки
                    rng = d.get("range", {})
                    st_col = rng.get("start", {}).get("character", 0)
                    en_col = rng.get("end", {}).get("character", st_col)
                    en_line = rng.get("end", {}).get("line", st_line)

                    if st_line == en_line and 0 <= st_col < len(line_text):
                        flagged = line_text[st_col:en_col].strip()
                        # Чистые символы Markdown (пайпы, двоеточия, дефисы и т.д.)
                        if not flagged or all(c in "|:-#*_`~>+[]()<>=!." for c in flagged):
                            continue
                        # Текст внутри инлайн-кода `...`
                        before = line_text[:st_col]
                        after = line_text[en_col:]
                        if before.count("`") % 2 == 1 and after.count("`") % 2 == 1:
                            continue

            combined.append(d)

        self.editor.set_diagnostics(combined)

    def update_file_path(self, new_path: str):
        was_read_only = self.editor_mode == 'read_only'
        last_edit_mode = self._last_editable_markdown_mode
        old_path = self.file_path
        from .text_alignment import save
        save(new_path, self.editor)
        self.file_path = os.path.abspath(new_path)
        if self.isVisible():
            from ui.history_focus import focus_document
            focus_document(self)
        self.selection_stats.schedule()

        self._ast_diagnostics.clear()
        self._lsp_diagnostics.clear()
        self.editor.set_diagnostics([])

        lsp_manager.notify_close(old_path)
        self.editor.set_file_path(self.file_path)
        if hasattr(self, "editor_host"):
            self.editor_host.update_editor_geometry()
        if self.highlighter is not None:
            try:
                self.highlighter.setDocument(None)
            except Exception:
                pass
            self.highlighter.deleteLater()
        is_md = Path(self.file_path).suffix.lower() in {".md", ".markdown"}
        self.editor_mode = "live" if is_md else "raw"
        self.editor.setReadOnly(False)
        self.editor.markdown_handler.set_enabled(is_md)
        self.editor.markdown_handler.set_visual_enabled(is_md)
        self.highlighter = get_highlighter_for_file(self.file_path, self.editor.document(), self.editor)
        self.content_stack.setCurrentWidget(self.editor_host)
        if was_read_only and is_md:
            self._set_markdown_mode('read_only')
            self._last_editable_markdown_mode = last_edit_mode
        else:
            self._last_editable_markdown_mode = self.editor_mode
            self.selection_stats_host.show()
            self.selection_stats.schedule()

        show_document_path(self.path_label, new_path)
        if not self._memory_only:
            from core.file_origins import watch, unwatch
            unwatch(self._origin_watch_path[0])
            self._origin_watch_path[0] = self.file_path
            watch(self.file_path)
            self._origin_revision += 1
            self._model_modified = False
            self.state_changed.emit()
        self._watch_current_file()

        root_dir = self.workspace_root
        if self.lsp_enabled and self._lsp_activated:
            lsp_manager.notify_open(self.file_path, self.editor.toPlainText(), root_dir)
            self._sync_lsp_changes()

    def load_file_content(self, *, acknowledge_external=True):
        if self._memory_only or os.path.exists(self.file_path):
            try:
                from ui.startup_data import read_startup_data
                if self._memory_only:content = self._memory_content
                else:content, self._text_format = read_startup_data(self.file_path, 'text_snapshot')
                # Reloading identical bytes used to erase the whole Undo stack
                # through setPlainText(), including a recent full-file paste.
                if not self._load_ok or self.editor.toPlainText() != content:
                    blocked = self.editor.blockSignals(True)
                    undo_enabled = self.editor.isUndoRedoEnabled()
                    try:
                        self.editor.setPlainText(content)
                        from .text_alignment import restore
                        self.editor.setUndoRedoEnabled(False)
                        restore(self.file_path, self.editor)
                        self.editor.document().setDocumentMargin(4)
                        self.editor.update_line_number_area_width(0)
                    finally:
                        self.editor.setUndoRedoEnabled(undo_enabled)
                        self.editor.blockSignals(blocked)
                # The file may be loaded before its stacked page receives its
                # final width.  Refresh once now and again when the page is
                # actually shown/activated.
                defer(0, self, self.refresh_editor_layout)

                self.editor.document().setModified(False)
                self.set_dirty(False)
                self._load_ok = True
                self._load_error = None
                self.editor.setReadOnly(self.editor_mode == 'read_only')
                self._disk_hash = self._hash_text(content)
                self._external_conflict = False
                self._model_modified = False
                self._origin_revision += 1
                self._watch_current_file()
                if acknowledge_external:
                    self._acknowledge_disk_version()
                else:
                    self.state_changed.emit()

                if self.editor.is_markdown:
                    self.editor.markdown_handler.parse_code_blocks()

                root_dir = self.workspace_root
                if self.lsp_enabled and self._lsp_activated:
                    lsp_manager.notify_open(self.file_path, content, root_dir)

            except Exception as e:
                self._load_error = str(e)
                if not self._load_ok:
                    self.editor.setPlainText(tr('ui.caption.db30724338f1', v0=e))
                    self.editor.document().setModified(False)
                    self.set_dirty(False)
                self.editor.setReadOnly(True)
                from core.logger import core_logger
                core_logger.error('FILE_READ_ERROR', f'{self.file_path}: {e}')

    def save_file(self, *, confirm_external=True, quiet=False):
        from core.logger import core_logger
        if self._load_error:
            if not quiet:QMessageBox.warning(self, tr('ui.file_view.read_error_6d9a'), tr('editor.read_protected'))
            return False
        if self._memory_only:
            self._memory_content = self.editor.toPlainText()
            self.editor.document().setModified(False)
            self.set_dirty(False)
            return True
        from .editor_sync import disk_version, conflict_choice
        try:
            current, _ = disk_version(self.file_path)
        except OSError as error:
            if not quiet:
                QMessageBox.warning(self, tr('ui.file_view.read_error_6d9a'), str(error))
            return False
        buffer_hash = self._hash_text(self.editor.toPlainText())
        self._external_conflict = current != self._disk_hash and current != buffer_hash
        if self._external_conflict:
            self.state_changed.emit()
            # Тихое сохранение и закрытие не подтверждают перезапись новой версии.
            if not confirm_external:
                return False
            answer = conflict_choice(self)
            if answer == 'reload':
                if current is not None:
                    self.load_file_content()
                return False
            if answer != 'overwrite':
                return False
            # За время открытого меню модель могла снова изменить файл.
            if disk_version(self.file_path)[0] != current:
                self._schedule_external_check()
                return False
        try:
            content = self.editor.toPlainText()
            write_text(self.file_path, content, text_format=self._text_format)
            from .text_alignment import save
            save(self.file_path, self.editor)
            self.editor.document().setModified(False)
            self.set_dirty(False)
            self._disk_hash = self._hash_text(content)
            self._acknowledge_disk_version()
            self._watch_current_file()

            self._sync_lsp_changes()
            if self.lsp_enabled:
                lsp_manager.notify_save(self.file_path, content)
            core_logger.event('storage', 'FILE_SAVE', self.file_path)
            return True
        except Exception as e:
            core_logger.event('storage', 'FILE_SAVE_ERROR', f'{self.file_path}: {e}', level='ERROR')
            if not quiet:
                QMessageBox.warning(self, tr('ui.file_view.save_error_354f'), tr('ui.file_view.could_not_save_file_v0_de16', v0=e))
            return False

    def _acknowledge_disk_version(self):
        from .editor_sync import disk_version
        self._origin_revision += 1
        self._external_conflict = False
        self._model_modified = False
        self._acknowledged_disk_oid = None
        if not self._memory_only:
            try:
                current, oid = disk_version(self.file_path)
                if current == self._disk_hash:
                    self._acknowledged_disk_oid = oid
                else:
                    self._schedule_external_check()
            except OSError:
                self._schedule_external_check()
        show_document_path(self.path_label, self.file_path)
        self.state_changed.emit()

    @staticmethod
    def _hash_text(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _watch_current_file(self):
        if self._memory_only:
            return
        parent = str(Path(self.file_path).parent)
        for path in self.file_watcher.files() + self.file_watcher.directories():
            if path not in (self.file_path, parent):
                self.file_watcher.removePath(path)
        if Path(parent).is_dir() and parent not in self.file_watcher.directories():
            self.file_watcher.addPath(parent)
        if Path(self.file_path).is_file() and self.file_path not in self.file_watcher.files():
            self.file_watcher.addPath(self.file_path)

    def _schedule_external_check(self, _path=None):
        if not self._memory_only:
            self._external_check_timer.start()

    def _on_external_file_changed(self, path):
        if os.path.abspath(path) == self.file_path:
            self._check_external_file()

    def _check_external_file(self):
        if self._memory_only:
            return
        from .editor_sync import disk_version
        self._watch_current_file()
        try:
            current, oid = disk_version(self.file_path)
        except OSError:
            return
        if current == self._disk_hash:
            if self._external_conflict:
                self._external_conflict = False
                self._model_modified = False
                self._origin_revision += 1
                show_document_path(self.path_label, self.file_path)
                self.state_changed.emit()
            return
        self._acknowledged_disk_oid = None
        if self.is_dirty or current is None:
            self._external_conflict = True
            show_document_path(self.path_label, self.file_path, tr('ui.caption.0c227000fd18'))
        else:
            cursor = self.editor.textCursor()
            position, anchor = cursor.position(), cursor.anchor()
            vertical = self.editor.verticalScrollBar().value()
            horizontal = self.editor.horizontalScrollBar().value()
            self.load_file_content(acknowledge_external=False)
            last = max(0, self.editor.document().characterCount() - 1)
            cursor = self.editor.textCursor()
            cursor.setPosition(min(anchor, last))
            cursor.setPosition(min(position, last), QTextCursor.KeepAnchor)
            self.editor.setTextCursor(cursor)
            self.editor.verticalScrollBar().setValue(vertical)
            self.editor.horizontalScrollBar().setValue(horizontal)
        self._model_modified = False
        self.state_changed.emit()
        self._query_file_origin(oid)
        # История инструмента фиксируется после завершения записи.
        defer(750, self, lambda: self._query_file_origin(oid))
        defer(2500, self, lambda: self._query_file_origin(oid))

    def _query_file_origin(self, oid):
        from ui.core_bridge.process_manager import core_process
        if not oid or oid == self._acknowledged_disk_oid or not core_process.is_running():
            return
        from .editor_sync import disk_version
        try:
            if disk_version(self.file_path)[1] != oid:
                return
        except OSError:
            return
        from ui.core_bridge.session_client import SessionApiClient
        revision, path = self._origin_revision, self.file_path
        def loaded(value):
            from shiboken6 import isValid
            if (not isValid(self) or revision != self._origin_revision or path != self.file_path
                    or oid == self._acknowledged_disk_oid):
                return
            try:
                if disk_version(path)[1] != oid:
                    return
            except OSError:
                return
            self._model_modified = bool(value.get('model'))
            self.state_changed.emit()
            chat = getattr(self.window(), 'chat_view', None)
            workers = list(getattr(chat, '_active_runs', {}).values())
            if not self._model_modified and any(worker.isRunning() for worker in workers):
                defer(2000, self, lambda: self._query_file_origin(oid))
        self._origin_runner.submit(lambda: SessionApiClient(core_process.base_url)._request('POST', '/v1/file-origin',
            json={'path': path, 'oid': oid}, timeout=30), loaded)

    @Slot(bool)
    def set_dirty(self, dirty: bool):
        dirty = bool(dirty)
        if dirty and self.editor_mode == 'read_only':
            self._preview_refresh_timer.start()
        if self.is_dirty != dirty:
            self.is_dirty = dirty
            # Keep save/close decisions synchronous, but notify the surrounding
            # UI after QTextDocument has finished its native edit/signal stack.
            # The child timer also cancels delivery when this tab is deleted.
            if not self._dirty_notification_timer.isActive():
                self._dirty_notification_timer.start()

    @Slot()
    def _emit_dirty_state(self):
        if self._notified_dirty != self.is_dirty:
            self._notified_dirty = self.is_dirty
            self.dirty_state_changed.emit(self.is_dirty)


# =========================================================================
# ДОКУМЕНТАЦИЯ И ВРЕМЕННЫЕ MARKDOWN-ДОКУМЕНТЫ
# =========================================================================

class TemporaryMarkdownTab(QWidget):
    """Documentation preview with an explicit, lazy source-editing mode."""
    dirty_state_changed = Signal(bool)
    agent_request_submitted = Signal(str, object)

    def __init__(self, title: str, content: str, breadcrumb: str = "Temporary", parent=None, *, base_path=None, immutable=False):
        super().__init__(parent)
        self.title = title if title.lower().endswith(".md") else f"{title}.md"
        self.file_path = str(base_path or '')
        self._base_path = base_path
        self._edit_document = None
        self._editing = False
        self._immutable = bool(immutable)
        self._agent_fragments = []
        self.agent_popup = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QFrame(self)
        toolbar.setObjectName("FileDocToolbar")
        toolbar.setFixedHeight(22)
        tb_layout = QHBoxLayout(toolbar)
        tb_layout.setContentsMargins(16, 2, 8, 2)
        self.path_label = QLabel(document_path(base_path) if base_path else breadcrumb, toolbar)
        self.path_label.setObjectName("FileDocBreadcrumb")
        tb_layout.addWidget(self.path_label)
        tb_layout.addStretch(1)
        layout.addWidget(toolbar)

        from .readonly_markdown import ReadOnlyMarkdownEditor
        self.editor = ReadOnlyMarkdownEditor(self, context_menu_handler=self._show_preview_context_menu)
        self.editor.setMaximumWidth(EDITOR_READING_WIDTH + 2 * CodeMinimap.WIDTH + EDITOR_LINE_NUMBER_GAP + EDITOR_MINIMAP_GAP)
        self.editor.set_markdown(content, base_path=base_path)
        self.editor_host = QWidget(self)
        editor_layout = QHBoxLayout(self.editor_host)
        editor_layout.setContentsMargins(0, 0, 0, 0)
        editor_layout.addWidget(self.editor, 1)
        self.content_stack = QStackedWidget(self)
        self.content_stack.addWidget(self.editor_host)
        layout.addWidget(self.content_stack, 1)
        self.shortcut_toggle = QShortcut(QKeySequence('Ctrl+Shift+0'), self)
        self.shortcut_toggle.setContext(Qt.WidgetWithChildrenShortcut)
        self.shortcut_toggle.activated.connect(self.toggle_editing)
        self.shortcut_toggle.setEnabled(not self._immutable)
        self.shortcut_save = QShortcut(QKeySequence.Save, self)
        self.shortcut_save.setContext(Qt.WidgetWithChildrenShortcut)
        self.shortcut_save.activated.connect(self.save_file)
        defer(0, self, self.refresh_editor_layout)

    def update_content(self, content: str, breadcrumb: Optional[str] = None, *, base_path=None):
        if self._edit_document is not None:
            if not self.is_dirty and self._edit_document.editor.toPlainText() != content:
                if self._edit_document._memory_only:
                    self._edit_document._memory_content = content
                self._edit_document.load_file_content()
            content = self._edit_document.editor.toPlainText()
        from .text_alignment import mapping
        alignments = mapping(self._edit_document.editor) if self._edit_document is not None else None
        self.editor.set_markdown(content, base_path=base_path, line_alignments=alignments)
        if breadcrumb is not None:
            bind_caption(self.path_label, document_path(base_path) if base_path else breadcrumb)
        self.refresh_editor_layout()
        defer(0, self, self.refresh_editor_layout)

    def refresh_editor_layout(self):
        if self._editing:
            self._edit_document.refresh_editor_layout()
            return
        self.editor.outline_rail.refresh_headings()
        self.editor.minimap.update()
        self.editor.viewport().update()

    @property
    def is_dirty(self):
        return self._edit_document.is_dirty if self._edit_document is not None else False

    @property
    def _external_conflict(self):
        return bool(self._edit_document and self._edit_document._external_conflict)

    def toggle_editing(self):
        if self._immutable:
            return
        if not self._editing:
            line = int(self.editor.minimap_viewport_rows()[0])
            if self._edit_document is None:
                memory_only = not bool(self._base_path)
                self._edit_document = FileDocumentTab(self._base_path or self.title, self,
                    memory_only=memory_only, content=self.editor.toPlainText(), allow_read_only_mode=False,
                    allow_file_path=False)
                self._edit_document._search_replace_allowed = False
                self._edit_document.toolbar.hide()
                self._edit_document.dirty_state_changed.connect(self.dirty_state_changed.emit)
                self._edit_document.agent_request_submitted.connect(self.agent_request_submitted.emit)
                self.content_stack.addWidget(self._edit_document)
            self._edit_document.editor.setReadOnly(False)
            self.content_stack.setCurrentWidget(self._edit_document)
            block = self._edit_document.editor.document().findBlockByNumber(line)
            if block.isValid():
                self._edit_document.editor.setTextCursor(QTextCursor(block))
                self._edit_document.editor.centerCursor()
            self._edit_document.editor.setFocus()
        else:
            source = self._edit_document.editor
            line = source.textCursor().blockNumber()
            self._edit_document.cancel_agent_prompt()
            source.setReadOnly(True)
            from .text_alignment import mapping
            self.editor.set_markdown(source.toPlainText(), base_path=self._base_path,
                                     line_alignments=mapping(source))
            self.content_stack.setCurrentWidget(self.editor_host)
            self.editor.minimap_scroll_to_row(line, center=True)
            self.editor.renderer.setFocus()
        self._editing = not self._editing
        self.shortcut_save.setEnabled(not self._editing)
        self.refresh_editor_layout()

    def save_file(self, **options):
        if self._edit_document is None or not self.is_dirty:
            return True
        return self._edit_document.save_file(**options)

    def cancel_agent_prompt(self):
        if self.agent_popup is not None:
            self.agent_popup.reset_and_hide()
            self.clear_agent_context()
        if self._edit_document is not None:
            self._edit_document.cancel_agent_prompt()

    def viewport(self):
        return self.editor.viewport()

    def toPlainText(self):
        return self.editor.toPlainText()

    def agent_context_fragments(self):
        return list(self._agent_fragments)

    def clear_agent_context(self):
        self._agent_fragments.clear()

    def _open_preview_agent(self, selected, anchor):
        if self.agent_popup is None:
            self.agent_popup = AgentSelectionPopup(self, lambda: '', parent=self.editor.viewport(),
                                                   allow_file_path=False)
            set_text(self.agent_popup.hint_label, 'ui.file_view.preview_agent_context_hint')
            self.agent_popup.submitted.connect(self._submit_preview_agent)
            self.agent_popup.cancelled.connect(self.clear_agent_context)
        self.agent_popup.reset_and_hide()
        self._agent_fragments = [selected] if selected else []
        self.agent_popup.open_at(anchor)

    def _submit_preview_agent(self, message, _file_context):
        self.cancel_agent_prompt()
        self.agent_request_submitted.emit(message, {})

    def _show_preview_context_menu(self, renderer, pos):
        source = renderer._last_context_widget
        if isinstance(source, QLabel):
            selected = source.selectedText()
        elif source is not None and hasattr(source, 'textCursor'):
            selected = source.textCursor().selectedText().replace('\u2029', '\n').replace('\u2028', '\n')
        else:
            selected = ''
        menu = renderer.createStandardContextMenu()
        menu.setObjectName('FileManagerContextMenu')
        row = QWidget(menu)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(6, 4, 6, 4); layout.setSpacing(5)
        anchor = self.editor.viewport().mapFromGlobal(renderer.mapToGlobal(pos))
        for icon, tooltip, callback in (
            ('sparkles', 'ui.file_view.open_agent_request_f4a1',
             lambda: self._open_preview_agent(selected, anchor)),
            ('search', 'ui.search_popup.search_180f', lambda: self.editor.open_search(anchor)),
        ):
            button = QToolButton(row)
            button.setObjectName('FileEditorAgentButton')
            set_icon(button, icon, square=True)
            set_text(button, tooltip, setter='setToolTip')
            button.setFixedSize(28, 28)
            apply_style(button, 'QToolButton {background:@background.surface@;border:1px solid @border.subtle@;border-radius:4px;padding:0;} QToolButton:hover {background:@control.hover@;}')
            def open_panel(_=False, callback=callback):
                menu.close()
                defer(0, self, callback)
            button.clicked.connect(open_panel)
            layout.addWidget(button)
        layout.addStretch(1)
        action = QWidgetAction(menu); action.setDefaultWidget(row)
        first = menu.actions()[0] if menu.actions() else None
        menu.insertAction(first, action)
        self._preview_context_menu = menu
        menu.exec(renderer.mapToGlobal(pos))
        menu.deleteLater()

    def discard_changes(self):
        if self._edit_document is not None:
            self._edit_document.load_file_content()
            self.editor.set_markdown(self._edit_document.editor.toPlainText(), base_path=self._base_path)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh_editor_layout()
        defer(0, self, self.refresh_editor_layout)


# =========================================================================
# РАБОЧАЯ ОБЛАСТЬ «ФАЙЛ»
# =========================================================================

class BuiltinDocumentationTab(TemporaryMarkdownTab):
    """Packaged help is never bound to an editable file document."""
    def __init__(self, title, content, breadcrumb='Built-in', parent=None, *, base_path=None):
        from ui.builtin_docs import document_path
        path = document_path(title)
        super().__init__(title, content, breadcrumb, parent,
                         base_path=str(path), immutable=True)
        from ui.i18n import i18n
        i18n.language_changed.connect(self._reload_language)

    def _reload_language(self, *_):
        from ui.builtin_docs import document_path
        try:
            path = document_path(self.title)
            text = path.read_text(encoding='utf-8-sig')
        except (OSError, UnicodeError, ValueError) as error:
            QMessageBox.warning(self, tr('ui.plugin_authoring_help.documentation_b123'), str(error))
            return
        self.cancel_agent_prompt()
        self.update_content(text, 'Built-in / ' + self.title, base_path=str(path))

    def update_content(self, content, breadcrumb=None, *, base_path=None):
        if base_path is not None:
            self._base_path = str(base_path)
            self.file_path = str(base_path)
        super().update_content(content, breadcrumb, base_path=base_path)


class PipelineHelpTab(BuiltinDocumentationTab):
    """Compatibility entry point for the pipeline editor's built-in help tab."""

class ImagePreviewTab(TemporaryMarkdownTab):
    """An image preview never opens binary bytes in a source-editing tab."""
    def __init__(self,title,content,breadcrumb="Image",parent=None,*,base_path=None):
        super().__init__(title,content,breadcrumb,parent,base_path=base_path)
        self.title = title
        self.shortcut_toggle.setEnabled(False)
        self.shortcut_save.setEnabled(False)
        from .inline_images import image_reference,resolve_image
        reference = image_reference(content.strip())
        self._image_source_path = resolve_image(reference[1],base_path) if reference else None
        if self._image_source_path and self._image_source_path.suffix.lower()=='.svg':
            self.shortcut_toggle.setEnabled(True)
            button = QToolButton(self.path_label.parentWidget())
            set_text(button, 'ui.file_view.edit_source_54bb')
            button.clicked.connect(self.toggle_editing)
            self.path_label.parentWidget().layout().addWidget(button)

    def toggle_editing(self):
        if self._image_source_path and self._image_source_path.suffix.lower()=='.svg':
            host = self.parentWidget()
            while host is not None and not hasattr(host,'open_docs'):
                host = host.parentWidget()
            if host is not None:
                host.open_file(str(self._image_source_path),force=True)


class FileView(QWidget):
    agent_request_submitted = Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("FileViewRoot")
        self.open_docs: Dict[str, FileDocumentTab] = {}
        self.virtual_docs: Dict[str, TemporaryMarkdownTab] = {}

        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        self.header_bar = QFrame(self)
        self.header_bar.setObjectName("FileEditorHeaderBar")
        self.header_bar.setFixedHeight(32)
        header_layout = QHBoxLayout(self.header_bar)
        header_layout.setContentsMargins(0, 0, 8, 0)
        header_layout.setSpacing(6)
        header_layout.setAlignment(Qt.AlignVCenter)

        self.menu_button = set_icon(QToolButton(self.header_bar), 'menu-2', square=True)
        self.menu_button.setObjectName('PipelineManagerToggle')
        self.menu_button.setFixedSize(28, 28)
        set_text(self.menu_button, 'ui.file_menu.title', setter='setToolTip')
        self.menu_button.clicked.connect(self._file_menu)
        header_layout.addWidget(self.menu_button)
        self._file_actions_menu = None

        self.tab_bar = FileTabBar(self.header_bar)
        self.tab_bar.currentChanged.connect(self._on_current_tab_changed)
        header_layout.addWidget(self.tab_bar)

        self.new_tab_btn = NewTabButton(self.header_bar)
        self._attach_new_file_menu(self.new_tab_btn)
        header_layout.addWidget(self.new_tab_btn)

        header_layout.addStretch(1)
        root_layout.addWidget(self.header_bar)

        self.editor_stack = QStackedWidget(self)
        self.editor_stack.setObjectName("FileEditorStack")

        from ui.widgets.empty_workspace import EmptyWorkspace
        self.no_files_frame = EmptyWorkspace(self, new_key='workspace.new_file',
            create=lambda: self._create_new_untitled_file('.md'), open_file=self._open_dialog)
        self.placeholder_new_btn = self.no_files_frame.new_button
        self.placeholder_open_btn = self.no_files_frame.open_button

        self.editor_stack.addWidget(self.no_files_frame)
        root_layout.addWidget(self.editor_stack, 1)

        self.header_bar.show()
        self.tab_memory = TabMemoryManager(
            self, "text", self.editor_stack, self.open_docs, FileDocumentTab, self._bind_document
        )

        from ui.views.project_text_tabs import ProjectTextTabs
        self.project_tabs = ProjectTextTabs(self)

        event_bus.open_file_requested.connect(self.open_file)
        event_bus.open_markdown_anchor_requested.connect(self.open_markdown_anchor)
        event_bus.file_renamed.connect(self.rename_file)

    def _bind_document(self, document):
        document.dirty_state_changed.connect(self._on_document_dirty_changed)
        document.agent_request_submitted.connect(self.agent_request_submitted.emit)

    def _current_document(self):
        document = self.tab_bar.tabData(self.tab_bar.currentIndex())
        if isinstance(document, SuspendedTab):
            document = self.tab_memory.ensure_loaded(document)
        return document if isinstance(document, (FileDocumentTab, TemporaryMarkdownTab)) else None

    def _file_menu(self):
        if self._file_actions_menu is not None:
            self._file_actions_menu.close()
            self._file_actions_menu.deleteLater()
        menu = QMenu(self.menu_button)
        menu.setObjectName('FileManagerContextMenu')
        self._file_actions_menu = menu
        document = self._current_document()
        editor = (document._edit_document.editor if isinstance(document, TemporaryMarkdownTab)
                  and document._edit_document is not None else getattr(document, 'editor', None))
        def action(key, callback, enabled=True):
            item = add_caption_action(menu, 'addAction', tr(key), callback)
            item.setEnabled(enabled)
            return item
        action('ui.file_menu.new', lambda: self._create_new_untitled_file('.md'))
        action('ui.paint_view.open_1f1d', self._open_dialog)
        menu.addSeparator()
        action('common.save', lambda: document.save_file(), document is not None and not isinstance(document, ImagePreviewTab))
        action('ui.file_menu.save_as', self._save_as, document is not None and not isinstance(document, ImagePreviewTab))
        action('ui.file_menu.save_all', self._save_all, bool(self.tab_bar.count()))
        menu.addSeparator()
        editable = editor is not None and hasattr(editor, 'isReadOnly') and not editor.isReadOnly()
        for key, method in [('ui.paint_view.undo_a828', 'undo'), ('ui.paint_view.redo_7427', 'redo'),
                            ('ui.file_menu.cut', 'cut'), ('ui.file_menu.copy', 'copy'),
                            ('ui.file_menu.paste', 'paste'), ('ui.file_menu.select_all', 'selectAll')]:
            callback = getattr(editor, method, None)
            action(key, callback or (lambda: None), callback is not None and (editable or method in {'copy', 'selectAll'}))
        menu.addSeparator()
        action('ui.file_menu.close_tab', self._close_current_tab, document is not None)
        menu.popup(self.menu_button.mapToGlobal(QPoint(0, self.menu_button.height())))

    def _open_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('ui.paint_view.open_1f1d'), str(app_state.workspace))
        for path in paths:
            self.open_file(path, force=True)

    def _save_all(self):
        for index in range(self.tab_bar.count()):
            document = self.tab_bar.tabData(index)
            if isinstance(document, (FileDocumentTab, TemporaryMarkdownTab)) and document.is_dirty:
                if not document.save_file():
                    return False
        return True

    def _save_as(self):
        document = self._current_document()
        if document is None or isinstance(document, ImagePreviewTab):
            return False
        if getattr(document, '_load_error', None) and not document._load_ok:
            QMessageBox.warning(self, tr('ui.file_view.read_error_6d9a'), tr('editor.read_protected'))
            return False
        path = getattr(document, 'file_path', '') or str(Path(app_state.workspace) / document.title)
        destination, _ = QFileDialog.getSaveFileName(self, tr('ui.file_menu.save_as'), path)
        if not destination:
            return False
        destination = os.path.abspath(destination)
        if isinstance(document, FileDocumentTab) and destination == document.file_path:
            return document.save_file()
        if destination in self.open_docs:
            QMessageBox.warning(self, tr('ui.file_menu.save_as'), tr('ui.file_menu.already_open'))
            return False
        source = (document._edit_document.editor if isinstance(document, TemporaryMarkdownTab)
                  and document._edit_document is not None else document.editor)
        text = source.toPlainText()
        try:
            write_text(destination, text, text_format=getattr(document, '_text_format', None))
            from .text_alignment import save
            save(destination, source)
        except OSError as error:
            QMessageBox.warning(self, tr('ui.file_view.save_error_354f'), str(error))
            return False
        if isinstance(document, FileDocumentTab):
            self.rename_file(document.file_path, destination)
            document._load_error=None
            document.editor.setReadOnly(document.editor_mode=='read_only')
            document._disk_hash = document._hash_text(text)
            document.editor.document().setModified(False)
            document.set_dirty(False)
            document._acknowledge_disk_version()
        else:
            self.open_file(destination, force=True)
        return True

    def _close_current_tab(self):
        document = self._current_document()
        if isinstance(document, FileDocumentTab):
            self._close_tab_by_path(document.file_path)
        elif document is not None:
            key = next((key for key, value in self.virtual_docs.items() if value is document), None)
            if key is not None:
                self._close_virtual_tab(key)

    @Slot(bool)
    def _on_document_dirty_changed(self, dirty: bool):
        document = self.sender()
        if isinstance(document, FileDocumentTab):
            self._update_tab_title(document.file_path, dirty)
        elif isinstance(document, TemporaryMarkdownTab):
            for index in range(self.tab_bar.count()):
                if self.tab_bar.tabData(index) is document:
                    bind_caption(self.tab_bar, ('* ' if dirty else '') + document.title, setter='setTabText', index=index)
                    break

    def _get_workspace_dir(self, suffix: str = "") -> Path:
        # Заметки создаются в материалах, исходники и обычный текст — в Project Folder.
        ws_dir = Path(app_state.view_workspace('materials') if suffix == '.md' else app_state.workspace)
        ws_dir.mkdir(parents=True, exist_ok=True)
        return ws_dir

    def _on_current_tab_changed(self, index: int):
        if index < 0 or index >= self.tab_bar.count():
            return
        doc_widget = self.tab_bar.tabData(index)
        if self.isVisible():
            from ui.history_focus import focus_document
            focus_document(doc_widget)
        if isinstance(doc_widget, SuspendedTab):
            doc_widget = self.tab_memory.ensure_loaded(doc_widget)
        if isinstance(doc_widget, (FileDocumentTab, TemporaryMarkdownTab)):
            if isinstance(doc_widget, FileDocumentTab) and doc_widget.workspace_root != app_state.workspace:
                doc_widget.workspace_root = app_state.workspace
                if doc_widget._lsp_activated and doc_widget.lsp_enabled:
                    lsp_manager.notify_close(doc_widget.file_path)
                    lsp_manager.notify_open(doc_widget.file_path, doc_widget.editor.toPlainText(), doc_widget.workspace_root)
            old_widget = self.editor_stack.currentWidget()
            if isinstance(old_widget, (FileDocumentTab, TemporaryMarkdownTab)) and old_widget is not doc_widget:
                old_widget.cancel_agent_prompt()
            self.editor_stack.setCurrentWidget(doc_widget)
            self.tab_memory.touch(doc_widget)
            doc_widget.refresh_editor_layout()
            defer(0, doc_widget, doc_widget.refresh_editor_layout)
            if isinstance(doc_widget, FileDocumentTab):
                doc_widget._schedule_external_check()

    def open_temporary_markdown(
        self, *, key: str, title: str, content: str, breadcrumb: str = "Temporary", base_path=None
    ):
        clean_key = str(key or "temporary_markdown")
        existing = self.virtual_docs.get(clean_key)
        if existing is not None:
            existing.update_content(content, breadcrumb, base_path=base_path)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) is existing:
                    bind_caption(self.tab_bar, ('* ' if existing.is_dirty else '') + existing.title, setter='setTabText', index=i)
                    self.tab_bar.setCurrentIndex(i)
                    break
            else:
                idx = self.tab_bar.addTab(('* ' if existing.is_dirty else '') + existing.title)
                self.tab_bar.setTabData(idx, existing)
                self._attach_virtual_close_button(idx, clean_key)
                self.tab_bar.setCurrentIndex(idx)
            self.editor_stack.setCurrentWidget(existing)
            self.header_bar.show()
            return

        factory = (PipelineHelpTab if clean_key == 'builtin_doc:pipelienes_help.md' else
                   BuiltinDocumentationTab if clean_key.startswith('builtin_doc:') else
                   ImagePreviewTab if clean_key.startswith('image_preview:') else TemporaryMarkdownTab)
        doc = factory(title, content, breadcrumb, self, base_path=base_path)
        doc.dirty_state_changed.connect(self._on_document_dirty_changed)
        doc.agent_request_submitted.connect(self.agent_request_submitted.emit)
        self.virtual_docs[clean_key] = doc
        display_title = title if title.lower().endswith(".md") else f"{title}.md"
        idx = self.tab_bar.addTab(display_title)
        self.tab_bar.setTabData(idx, doc)
        self._attach_virtual_close_button(idx, clean_key)
        self.editor_stack.addWidget(doc)
        self.tab_bar.setCurrentIndex(idx)
        self.editor_stack.setCurrentWidget(doc)
        self.header_bar.show()
        defer(0, doc, doc.refresh_editor_layout)

    def _attach_virtual_close_button(self, idx: int, key: str):
        btn = TabCloseButton(self.tab_bar)
        btn.clicked.connect(lambda _checked=False, k=key: self._close_virtual_tab(k))
        self.tab_bar.setTabButton(idx, QTabBar.RightSide, btn)

    def _close_virtual_tab(self, key: str):
        doc = self.virtual_docs.get(str(key))
        if doc is None:
            return
        if doc.is_dirty:
            from ui.widgets.unsaved_popup import confirm_close
            if not confirm_close(self, doc, title=doc.title, tab_bar=self.tab_bar,
                    save=doc.save_file, discard=lambda: doc.discard_changes()
                    if self.project_tabs.referenced_elsewhere('virtual', str(key)) else None):
                return
        doc.cancel_agent_prompt()
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) is doc:
                self.tab_bar.removeTab(i)
                break
        if not self.project_tabs.referenced_elsewhere('virtual', str(key)):
            if doc._edit_document is not None and doc._edit_document._lsp_activated:
                lsp_manager.notify_close(doc._edit_document.file_path)
            self.virtual_docs.pop(str(key), None)
            self.editor_stack.removeWidget(doc)
            doc.deleteLater()
        if self.tab_bar.count() == 0:
            self.header_bar.show()
            self.editor_stack.setCurrentWidget(self.no_files_frame)

    def open_image_file(self,file_path):
        path = Path(file_path).resolve()
        if path.suffix.lower()=='.svg':
            self.open_file(str(path),force=True)
            document = self.open_docs.get(str(path))
            if isinstance(document,FileDocumentTab):
                document.show_svg_image(True)
            return
        from PySide6.QtCore import QUrl
        source = '![' + path.stem.replace(']','') + '](<' + QUrl.fromLocalFile(str(path)).toString() + '>)'
        self.open_temporary_markdown(key='image_preview:'+str(path),title=path.name,
            content=source,breadcrumb=str(path))

    def open_markdown_anchor(self, file_path, fragment):
        self.open_file(file_path)
        def navigate():
            from .markdown_anchors import resolve_anchor
            document = self.open_docs.get(os.path.abspath(file_path))
            if document is None:
                for candidate in self.virtual_docs.values():
                    renderer = getattr(getattr(candidate,'editor',None),'renderer',None)
                    if renderer is not None and str(renderer.base_path) == str(file_path):
                        document = candidate
                        break
            if document is None or not hasattr(document,'editor'):
                return
            editor = document.editor
            line = resolve_anchor(editor.toPlainText(),fragment)
            if line is None:
                from ui.widgets.inline_editor import notice
                notice(self,tr('ui.file_view.link_f4cb'),tr('ui.caption.3475e3ab71e9')+fragment)
                return
            preview = getattr(document,'_read_only_view',None)
            if getattr(document,'editor_mode',None) == 'read_only' and preview is not None:
                preview.navigate_to_heading(line)
            elif hasattr(editor,'navigate_to_heading'):
                editor.navigate_to_heading(line)
        defer(0,self,navigate)

    def open_file(self, file_path: str, *, force=False):
        from ui.startup_process import report_startup_phase
        report_startup_phase(tr('ui.caption.19dc69b07656') + Path(file_path).name)
        if not force and Path(file_path).suffix.lower() in {".tpaint", ".png", ".jpg", ".jpeg"}:
            return
        # Ordinary activation routes .canvas to Board; Open in File is explicit.
        if not force and file_path.lower().endswith(".canvas"):
            return

        abs_path = os.path.abspath(file_path)
        if not force and abs_path.lower().endswith(".canvas"):
            return

        resolved_path = Path(abs_path).resolve()
        from ui.builtin_docs import filename_for_path, document_path
        builtin_name = filename_for_path(resolved_path)
        if builtin_name is not None:
            try:
                resolved_path = document_path(builtin_name)
                content = resolved_path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError, ValueError):
                return
            self.open_temporary_markdown(
                key=f"builtin_doc:{resolved_path.name}",
                title=resolved_path.name,
                content=content,
                breadcrumb=f"Built-in / {resolved_path.name}",
                base_path=str(resolved_path),
            )
            return

        if abs_path in self.open_docs:
            doc_widget = self.open_docs[abs_path]
            if isinstance(doc_widget,FileDocumentTab) and getattr(doc_widget,'_svg_preview_active',False):
                doc_widget.show_svg_image(False)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) is doc_widget:
                    self.tab_bar.setCurrentIndex(i)
                    self._on_current_tab_changed(i)
                    return
            # Parked project documents remain alive, including dirty buffers.
            idx = self.tab_bar.addTab(('* ' if doc_widget.is_dirty else '') + Path(abs_path).name)
            self.tab_bar.setTabData(idx, doc_widget)
            self._attach_close_button(idx, abs_path)
            self.tab_bar.setCurrentIndex(idx)
            self._on_current_tab_changed(idx)
            self.header_bar.show()
            return

        doc_tab = FileDocumentTab(abs_path, self)
        self.tab_memory.bind_document(doc_tab)

        file_name = Path(abs_path).name
        idx = self.tab_bar.addTab(file_name)
        self.tab_bar.setTabData(idx, doc_tab)
        self.open_docs[abs_path] = doc_tab

        self._attach_close_button(idx, abs_path)

        self.editor_stack.addWidget(doc_tab)
        self.tab_bar.setCurrentIndex(idx)
        self.editor_stack.setCurrentWidget(doc_tab)
        self.tab_memory.touch(doc_tab)
        self.tab_bar.updateGeometry()
        self.header_bar.show()
        defer(0, doc_tab, doc_tab.refresh_editor_layout)

    def _attach_close_button(self, idx: int, file_path: str):
        btn = TabCloseButton(self.tab_bar)
        btn.clicked.connect(lambda _, p=file_path: self._close_tab_by_path(p))
        self.tab_bar.setTabButton(idx, QTabBar.RightSide, btn)

    def _close_tab_by_path(self, file_path: str):
        if file_path not in self.open_docs:
            return

        doc_tab = self.open_docs[file_path]
        if doc_tab.is_dirty:
            from ui.widgets.unsaved_popup import confirm_close
            if not confirm_close(self, doc_tab, title=Path(file_path).name, tab_bar=self.tab_bar,
                    save=doc_tab.save_file, discard=lambda: doc_tab.load_file_content()
                    if self.project_tabs.referenced_elsewhere('file', file_path) else None):
                return

        tab_idx = -1
        for i in range(self.tab_bar.count()):
            if self.tab_bar.tabData(i) == doc_tab:
                tab_idx = i
                break

        if tab_idx != -1:
            self.tab_bar.removeTab(tab_idx)
            self.tab_bar.updateGeometry()

        if not self.project_tabs.referenced_elsewhere('file', file_path):
            self.tab_memory.forget(doc_tab)
            self.open_docs.pop(file_path)
            lsp_manager.notify_close(file_path)
            self.editor_stack.removeWidget(doc_tab)
            doc_tab.deleteLater()

        for i in range(self.tab_bar.count()):
            w = self.tab_bar.tabData(i)
            if isinstance(w, (FileDocumentTab, SuspendedTab)):
                self._attach_close_button(i, w.file_path)

        if self.tab_bar.count() == 0:
            self.header_bar.show()
            self.editor_stack.setCurrentWidget(self.no_files_frame)

    def _attach_new_file_menu(self, button: QToolButton):
        menu = QMenu(button)
        for label, suffix in ((".md", ".md"), (".txt", ".txt"), (".py", ".py"), ("empty", "")):
            action = add_caption_action(menu, 'addAction', label)
            action.triggered.connect(lambda _checked=False, ext=suffix: self._create_new_untitled_file(ext))

        if not isinstance(button, NewTabButton):
            button.setMenu(menu)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        if isinstance(button, NewTabButton):
            button.menu_requested.connect(
                lambda b=button, m=menu: m.popup(b.mapToGlobal(QPoint(0, b.height())))
            )
        else:
            button.clicked.connect(
                lambda _checked=False, b=button, m=menu: m.popup(b.mapToGlobal(QPoint(0, b.height())))
            )

    def _create_new_untitled_file(self, suffix: str = ".md"):
        suffix = suffix if suffix in {".md", ".txt", ".py", ""} else ".md"
        ws_dir = self._get_workspace_dir(suffix)
        idx_count = 1
        while True:
            candidate = ws_dir / f"Untitled_{idx_count}{suffix}"
            if not candidate.exists() and str(candidate) not in self.open_docs:
                new_file = candidate
                break
            idx_count += 1

        # The selector chooses the file type, not a template: every new file
        # starts empty, including Markdown.
        write_text(new_file, "")

        self.open_file(str(new_file))

    def _update_tab_title(self, file_path: str, is_dirty: bool):
        if file_path in self.open_docs:
            doc_widget = self.open_docs[file_path]
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_widget:
                    base_name = Path(file_path).name
                    title = f"* {base_name}" if is_dirty else base_name
                    bind_caption(self.tab_bar, title, setter='setTabText', index=i)
                    break

    def rename_file(self, old_path: str, new_path: str):
        old_abs = os.path.abspath(old_path)
        new_abs = os.path.abspath(new_path)
        self.project_tabs.rename(old_abs, new_abs)

        if old_abs in self.open_docs:
            doc_tab = self.open_docs.pop(old_abs)
            self.open_docs[new_abs] = doc_tab
            doc_tab.update_file_path(new_abs)

            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_tab:
                    new_name = Path(new_abs).name
                    title = f"* {new_name}" if doc_tab.is_dirty else new_name
                    bind_caption(self.tab_bar, title, setter='setTabText', index=i)
                    self._attach_close_button(i, new_abs)
                    break
            return

        prefix = old_abs + os.sep
        keys_to_update = [k for k in list(self.open_docs.keys()) if k.startswith(prefix)]
        for old_f in keys_to_update:
            rel = os.path.relpath(old_f, old_abs)
            new_f = os.path.abspath(os.path.join(new_abs, rel))
            doc_tab = self.open_docs.pop(old_f)
            self.open_docs[new_f] = doc_tab
            doc_tab.update_file_path(new_f)
            for i in range(self.tab_bar.count()):
                if self.tab_bar.tabData(i) == doc_tab:
                    self._attach_close_button(i, new_f)
                    break

    def set_project(self, project_id):
        self.project_tabs.switch(project_id)

    def get_project_states(self):
        return self.project_tabs.export()

    def restore_project_states(self, states, project_id='', *, legacy=False):
        self.project_tabs.restore(states, project_id, legacy=legacy)

    def get_open_state(self) -> Dict[str, Any]:
        paths = []
        for i in range(self.tab_bar.count()):
            w = self.tab_bar.tabData(i)
            if isinstance(w, (FileDocumentTab, SuspendedTab)):
                paths.append(w.file_path)

        return {
            "open_files": paths,
            "active_file_index": self.tab_bar.currentIndex()
        }

    def confirm_close_all(self) -> bool:
        for doc_tab in [*self.open_docs.values(), *self.virtual_docs.values()]:
            if not doc_tab.is_dirty:
                continue
            answer = QMessageBox.question(
                self,
                tr('ui.file_view.unsaved_changes_136a'),
                tr('ui.file_view.save_changes_to_v0_before_exiting_d778', v0=getattr(doc_tab, 'title', '') or Path(doc_tab.file_path).name),
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                return False
            if answer == QMessageBox.Save and not doc_tab.save_file():
                return False
        return True

    def restore_open_state(self, open_files: List[str], active_index: int = 0):
        if not open_files:
            return

        for f_path in open_files:
            if os.path.exists(f_path) and os.path.isfile(f_path):
                self.open_file(f_path)

        if 0 <= active_index < self.tab_bar.count():
            self.tab_bar.setCurrentIndex(active_index)

    def showEvent(self, event):
        super().showEvent(event)
        self.tab_bar.updateGeometry()
        self.header_bar.updateGeometry()
