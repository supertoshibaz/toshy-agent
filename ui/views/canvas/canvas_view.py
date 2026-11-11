from ui.widgets.workspace_menu import standardize_workspace_menu, WorkspaceContextMenu
from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style
from ui.icons import set_icon, symbol_button

from ui.file_saving import write_text
import os
import math
import json
import uuid
import time
from collections import deque
from bisect import bisect_left, bisect_right, insort
from pathlib import Path
from typing import Optional, List, Dict, Set, Tuple, Any
from PySide6.QtWidgets import (
    QGraphicsView, QGraphicsScene, QGraphicsItem, QMenu, QFrame,
    QVBoxLayout, QHBoxLayout, QToolButton, QApplication,
    QWidget, QLabel, QLineEdit, QPushButton, QWidgetAction, QCheckBox, QSpinBox, QFormLayout
)
from PySide6.QtCore import (
    Qt, QRectF, QPointF, QPoint, Signal, QLineF, QMimeData, QByteArray,
    QSaveFile, QIODevice, QTimer, QEvent,
)
from PySide6.QtGui import (
    QPainter, QColor, QPen, QBrush, QWheelEvent,
    QMouseEvent, QUndoStack, QUndoCommand, QResizeEvent,
    QKeySequence, QShortcut, QKeyEvent, QCursor, QTextCursor
)

from .canvas_card import CanvasCardItem, GRID_SIZE
from .canvas_arrow import CanvasArrowItem
from ui.app_state import app_state
from ui.views.chat_view import TagsBarWidget
from ui.widgets.search_popup import SearchReplacePopup
from ui.tab_memory import performance_preferences

class ChangeZOrderCommand(QUndoCommand):
    """Команда отката изменения z-порядка (слоев) карточек."""
    def __init__(self, card_z_changes: list, scene):
        super().__init__("Изменить порядок слоев")
        self.card_z_changes = card_z_changes  # [(card, old_z, new_z), ...]
        self.scene = scene

    def redo(self):
        for card, _, new_z in self.card_z_changes:
            card.setZValue(new_z)
        self.scene.update()

    def undo(self):
        for card, old_z, _ in self.card_z_changes:
            card.setZValue(old_z)
        self.scene.update()

class ChangeCardsColorCommand(QUndoCommand):
    """Групповой откат изменения цвета карточек."""
    def __init__(self, card_colors: list, scene):
        super().__init__("Изменить цвет карточек")
        self.card_colors = card_colors  # [(card, old_color, new_color), ...]
        self.scene = scene

    def redo(self):
        for card, _, new_c in self.card_colors:
            card.set_color(new_c)
        self.scene.update()

    def undo(self):
        for card, old_c, _ in self.card_colors:
            card.set_color(old_c)
        self.scene.update()


class ChangeCardsAlignCommand(QUndoCommand):
    """Групповой откат изменения выравнивания текста."""
    def __init__(self, card_aligns: list, scene):
        super().__init__("Изменить выравнивание текста")
        self.card_aligns = card_aligns  # [(card, old_align, new_align), ...]
        self.scene = scene

    def redo(self):
        for card, _, new_a in self.card_aligns:
            card.set_text_align(new_a)
        self.scene.update()

    def undo(self):
        for card, old_a, _ in self.card_aligns:
            card.set_text_align(old_a)
        self.scene.update()


class CanvasAgentPopup(QFrame):
    """Floating Board -> Agent request composer.

    The popup only contains the user's prompt and context controls.  Selected
    cards/edges are kept by CanvasView and serialized only when Send is pressed.
    """

    submitted = Signal(str)
    cancelled = Signal()

    def __init__(self, view: 'CanvasView', parent=None):
        super().__init__(parent or view.viewport())
        self.view = view
        self.setObjectName("CanvasAgentPopup")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMinimumWidth(300)
        self.setMaximumWidth(440)
        apply_style(self, '\n            QFrame#CanvasAgentPopup {\n                background-color: @background.panel@;\n                border: 1px solid @border.subtle@;\n                border-radius: 8px;\n            }\n            QLabel#CanvasAgentHint {\n                color: @text.primary@;\n                background: transparent;\n                font-size: 11px;\n            }\n            QLabel#CanvasAgentSession {\n                color: @text.disabled@;\n                background: transparent;\n                font-size: 10px;\n            }\n            QLineEdit#CanvasAgentInput {\n                color: @text.primary@;\n                background-color: @background.window@;\n                border: 1px solid @border.subtle@;\n                border-radius: 5px;\n                padding: 6px 8px;\n                selection-background-color: @control.selected@;\n            }\n            QLineEdit#CanvasAgentInput:focus { border-color: @text.disabled@; }\n            QToolButton#CanvasAgentSend, QToolButton#CanvasAgentOption {\n                color:@text.primary@;background:transparent;border:1px solid @border.subtle@;\n                border-radius:6px;padding:0;min-width:30px;max-width:30px;min-height:30px;max-height:30px;\n            }\n            QToolButton#CanvasAgentSend:hover, QToolButton#CanvasAgentOption:hover {background:@control.hover@;}\n            QToolButton#CanvasAgentOption:checked {background:@control.selected@;}\n            QToolButton#CanvasAgentClose {\n                color: @text.secondary@;\n                background: transparent;\n                border:1px solid @border.subtle@;\n                border-radius: 4px;\n                font-size: 15px;\n                padding: 0;\n            }\n            QToolButton#CanvasAgentClose:hover {\n                color: @text.primary@;\n                background-color: @control.hover@;\n            }\n        ')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        self.hint_label = localize(QLabel(
            '',
            self,
        ), 'ui.canvas_view.select_cards_to_add_them_to_1874')
        self.hint_label.setObjectName("CanvasAgentHint")
        self.hint_label.setWordWrap(True)
        self.hint_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        header.addWidget(self.hint_label, 1)

        self.close_button = QToolButton(self)
        self.close_button.setObjectName("CanvasAgentClose")
        set_icon(self.close_button, 'close', size=18)
        set_text(self.close_button, 'ui.top_bar.close_a7a4', setter='setToolTip')
        self.close_button.setFixedSize(32, 32)
        self.close_button.setCursor(Qt.PointingHandCursor)
        self.close_button.clicked.connect(self.cancel)
        header.addWidget(self.close_button, 0, Qt.AlignTop | Qt.AlignRight)
        layout.addLayout(header)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(6)
        self.prompt_input = QLineEdit(self)
        self.prompt_input.setObjectName("CanvasAgentInput")
        set_text(self.prompt_input, 'ui.canvas_view.message_the_agent_2c9e', setter='setPlaceholderText')
        self.prompt_input.returnPressed.connect(self._submit)
        input_row.addWidget(self.prompt_input, 1)

        self.send_button = QToolButton(self)
        self.send_button.setObjectName("CanvasAgentSend")
        set_icon(self.send_button, 'send', size=20)
        self.send_button.setFixedSize(32, 32)
        set_text(self.send_button, 'ui.canvas_view.send_ceb6', setter='setToolTip')
        self.send_button.setCursor(Qt.PointingHandCursor)
        self.send_button.clicked.connect(self._submit)
        input_row.addWidget(self.send_button)
        layout.addLayout(input_row)

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(6)

        self.pair_edges_button = QToolButton(self)
        self.pair_edges_button.setObjectName("CanvasAgentOption")
        set_icon(self.pair_edges_button, 'arrows-left-right', square=True)
        self.pair_edges_button.setCheckable(True)
        set_text(self.pair_edges_button, 'ui.canvas_view.include_the_connection_between_paired_cards_6c2c', setter='setToolTip')
        self.pair_edges_button.setCursor(Qt.PointingHandCursor)
        self.pair_edges_button.toggled.connect(self.view.set_agent_pair_connections_enabled)
        controls.addWidget(self.pair_edges_button)

        self.tree_button = QToolButton(self)
        self.tree_button.setObjectName("CanvasAgentOption")
        set_icon(self.tree_button, 'sitemap', square=True)
        self.tree_button.setCheckable(True)
        set_text(self.tree_button, 'ui.canvas_view.include_the_entire_tree_starting_from_26e5', setter='setToolTip')
        self.tree_button.setCursor(Qt.PointingHandCursor)
        self.tree_button.toggled.connect(self.view.set_agent_tree_mode_enabled)
        controls.addWidget(self.tree_button)

        self.clear_button = QToolButton(self)
        self.clear_button.setObjectName("CanvasAgentOption")
        set_icon(self.clear_button, 'backspace', square=True)
        set_text(self.clear_button, 'ui.canvas_view.clear_agent_context_selection_351c', setter='setToolTip')
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.clicked.connect(self.view.clear_agent_context)
        controls.addWidget(self.clear_button)

        from ui.views.chat_input_controls import ChatIconButton
        self.tags_button = ChatIconButton('tag', tr('ui.canvas_view.tags_1331'), self)
        self.tags_button.setObjectName("ChatInputAuxBtn")
        apply_style(self.tags_button, 'QPushButton {background:transparent;border:1px solid @border.subtle@;border-radius:6px;padding:0;min-width:30px;max-width:30px;min-height:30px;max-height:30px;} QPushButton:hover {background:@control.hover@;} QPushButton[active="true"] {background:@control.selected@;}')
        self.tags_button.setCursor(Qt.PointingHandCursor)
        self.tags_button.clicked.connect(self._toggle_tags_panel)
        controls.addWidget(self.tags_button)

        controls.addStretch(1)
        self.session_label = QLabel(self)
        self.session_label.setObjectName("CanvasAgentSession")
        controls.addWidget(self.session_label, 0, Qt.AlignRight | Qt.AlignVCenter)
        layout.addLayout(controls)

        app_state.session_changed.connect(self._refresh_session_label)
        app_state.titles_changed.connect(self._catalog_title_changed)
        self._refresh_session_label(app_state.session_id or "", app_state.branch)

        # Same shared tag state as Chat and File Editor.  This is a sibling of
        # the popup so wrapping tags never changes the popup's own layout.
        self.tags_bar = TagsBarWidget(self.parentWidget())
        self.tags_bar.installEventFilter(self)
        self.tags_bar.tag_state_changed.connect(lambda: QTimer.singleShot(0, self._position_tags_bar))
        self.tags_bar.hide()
        self.tags_button.set_active(False)

        self._drag_start_global: Optional[QPoint] = None
        self._drag_start_pos: Optional[QPoint] = None

        self._escape_shortcut = QShortcut(QKeySequence("Esc"), self)
        self._escape_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._escape_shortcut.activated.connect(self.cancel)

        self.hide()

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

    def _catalog_title_changed(self):
        self._refresh_session_label(app_state.session_id, app_state.branch)

    def _refresh_session_label(self, session_id: str, _branch: str = "main"):
        full = app_state.display_session(session_id, _branch)
        visible = full if len(full) <= 22 else full[:19] + "…"
        set_text(self.session_label, 'ui.canvas_view.session_v0_7b37', v0=visible)
        bind_caption(self.session_label, full, setter='setToolTip')

    def _toggle_tags_panel(self):
        will_show = not self.tags_bar.isVisible()
        self.tags_bar.setVisible(will_show)
        self.tags_button.set_active(will_show)
        if will_show:
            self._position_tags_bar(adjust_popup=True)
            QTimer.singleShot(0, lambda: self._position_tags_bar(adjust_popup=True))

    def _position_tags_bar(self, *, adjust_popup: bool = False):
        from shiboken6 import isValid
        if not isValid(self) or not isValid(self.tags_bar):
            return
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
                max_y = max(6, parent.height() - self.height() - 6)
                self.move(self.x(), min(max_y, self.y() + needed))

        x = max(0, min(self.x(), max(0, parent.width() - width)))
        y = max(0, self.y() - panel_height - gap)
        self.tags_bar.setGeometry(x, y, width, panel_height)
        self.tags_bar.show()
        self.raise_()
        self.tags_bar.raise_()

    def open_at(self, anchor: QPoint):
        parent = self.parentWidget()
        if parent is None:
            return

        # `anchor` is always supplied in viewport coordinates.  The popup is
        # intentionally parented to CanvasView rather than viewport():
        # QAbstractScrollArea scrolls viewport children while panning the
        # scene, which made the composer drift together with the board.
        if parent is self.view:
            anchor = self.view.viewport().mapTo(parent, QPoint(anchor))

        self.adjustSize()
        width = min(440, max(300, parent.width() - 12))
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
        self.pair_edges_button.setChecked(False)
        self.tree_button.setChecked(False)
        self.tags_bar.hide()
        self.tags_button.set_active(False)
        self.hide()

    def cancel(self):
        self.reset_and_hide()
        self.cancelled.emit()

    def _submit(self):
        # An empty prompt is valid when cards are selected: the structured
        # canvas fragment itself is still meaningful input for the agent.
        if self.prompt_input.text().strip() or self.view.has_agent_context():
            self.submitted.emit(self.prompt_input.text().strip())

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            target = self.childAt(event.position().toPoint())
            interactive = isinstance(target, (QLineEdit, QToolButton, QPushButton, QCheckBox))
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
                candidate.setX(max(0, min(candidate.x(), max(0, parent.width() - self.width()))))
                candidate.setY(max(0, min(candidate.y(), max(0, parent.height() - self.height()))))
            self.move(candidate)
            self._position_tags_bar()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and self._drag_start_global is not None:
            self._drag_start_global = None
            self._drag_start_pos = None
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def moveEvent(self, event):
        super().moveEvent(event)
        QTimer.singleShot(0, self._position_tags_bar)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        QTimer.singleShot(0, self._position_tags_bar)

class CanvasSelectionToolbar(QFrame):
    """Всплывающая панель действий, появляющаяся над выделенной нодой или группой нод."""
    def __init__(self, view: 'CanvasView', parent=None):
        super().__init__(parent or view)
        self.view = view
        self.setObjectName("CanvasSelectionToolbar")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.hide()

        apply_style(self, """
            #CanvasSelectionToolbar {
                background-color: @background.window@;
                border: 1px solid @background.surface@;
                border-radius: 6px;
            }
            #CanvasSelectionToolbar QToolButton {
                background-color: transparent;
                border: none;
                border-radius: 4px;
                color: @text.primary@;
                font-size: 12px;
                min-width: 26px;
                max-width: 26px;
                min-height: 26px;
                max-height: 26px;
                padding: 0px;
            }
            #CanvasSelectionToolbar QToolButton:hover {
                background-color: @control.hover@;
                color: @text.primary@;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(3)

        # 1. Запрос агенту
        self.btn_agent = QToolButton(self)
        set_icon(self.btn_agent, 'sparkles', square=True)
        set_text(self.btn_agent, 'ui.canvas_view.add_selected_cards_to_an_agent_21f2', setter='setToolTip')
        self.btn_agent.clicked.connect(self.view.open_agent_from_selection)
        layout.addWidget(self.btn_agent)

        # 2. Удаление
        self.btn_del = QToolButton(self)
        symbol_button(self.btn_del, "🗑️")
        set_text(self.btn_del, 'ui.canvas_view.delete_selection_6a32', setter='setToolTip')
        self.btn_del.clicked.connect(self.view._delete_selected_cards)
        layout.addWidget(self.btn_del)

        # 3. Выравнивание текста
        self.btn_align = QToolButton(self)
        set_icon(self.btn_align, 'list', square=True)
        set_text(self.btn_align, 'ui.canvas_view.text_alignment_9434', setter='setToolTip')
        self.btn_align.clicked.connect(self._show_align_menu)
        layout.addWidget(self.btn_align)

        # 4. Палитра цветов
        self.btn_color = QToolButton(self)
        set_icon(self.btn_color, 'theme', square=True)
        set_text(self.btn_color, 'ui.canvas_view.change_color_712a', setter='setToolTip')
        self.btn_color.clicked.connect(self._show_color_palette)
        layout.addWidget(self.btn_color)

        # 5. Отправить вперед
        self.btn_front = QToolButton(self)
        set_icon(self.btn_front, 'arrow-bar-to-up', square=True)
        set_text(self.btn_front, 'ui.canvas_view.bring_to_front_867f', setter='setToolTip')
        self.btn_front.clicked.connect(self.view.bring_to_front)
        layout.addWidget(self.btn_front)

        # 6. Отправить на фон
        self.btn_back = QToolButton(self)
        set_icon(self.btn_back, 'arrow-bar-to-down', square=True)
        set_text(self.btn_back, 'ui.canvas_view.send_to_back_c6a0', setter='setToolTip')
        self.btn_back.clicked.connect(self.view.send_to_back)
        layout.addWidget(self.btn_back)

        # 7. Приближение к выделению
        self.btn_zoom = QToolButton(self)
        symbol_button(self.btn_zoom, "🔍")
        set_text(self.btn_zoom, 'ui.canvas_view.zoom_to_selection_732f', setter='setToolTip')
        self.btn_zoom.clicked.connect(self.view.zoom_to_selection)
        layout.addWidget(self.btn_zoom)

    def _show_color_palette(self):
        from ui.widgets.colour_palette import ColourPalette
        previous = getattr(self, '_color_menu', None)
        if previous is not None:
            previous.close()
            previous.deleteLater()
        menu = QMenu(self)
        menu.setObjectName("CanvasColorMenu")
        selected = [card for card in self.view.canvas_scene.selectedItems() if isinstance(card, CanvasCardItem)]
        palette = ColourPalette(selected[0].color if selected else None, menu)
        action = QWidgetAction(menu)
        action.setDefaultWidget(palette)
        menu.addAction(action)
        def apply(colour):
            self.view.set_selected_cards_color(colour)
            menu.close()
        palette.applied.connect(apply)
        self._color_menu = menu
        menu.popup(self.btn_color.mapToGlobal(QPoint(0, self.btn_color.height() + 4)))

    def _show_align_menu(self):
        menu = QMenu(self)
        menu.setObjectName("CanvasAlignMenu")

        act_left = localize(add_caption_action(menu, 'addAction', "По левому краю"), 'ui.canvas_view.align_left_d4e4')
        act_center = localize(add_caption_action(menu, 'addAction', "По центру"), 'ui.canvas_view.align_center_93f3')
        act_right = localize(add_caption_action(menu, 'addAction', "По правому краю"), 'ui.canvas_view.align_right_7317')
        act_card_center = localize(add_caption_action(menu, 'addAction', "По центру карточки"), 'ui.canvas_view.center_within_card_787a')

        act_left.triggered.connect(lambda: self.view.set_selected_cards_align("left"))
        act_center.triggered.connect(lambda: self.view.set_selected_cards_align("center"))
        act_right.triggered.connect(lambda: self.view.set_selected_cards_align("right"))
        act_card_center.triggered.connect(lambda: self.view.set_selected_cards_align("card_center"))

        menu.exec(self.btn_align.mapToGlobal(QPoint(0, self.btn_align.height() + 4)))

class AddCardCommand(QUndoCommand):
    def __init__(self, scene, card, text=""):
        super().__init__(tr('ui.canvas_view.add_card_3c1b'))
        self.scene = scene
        self.card = card
        self.initial_text = text

    def redo(self):
        if self.card.scene() is not self.scene:
            self.scene.addItem(self.card)
            if self.initial_text:
                self.card.editor.setPlainText(self.initial_text)

    def undo(self):
        self.scene.removeItem(self.card)


class DeleteCardCommand(QUndoCommand):
    def __init__(self, scene, card):
        super().__init__("Удалить карточку")
        self.scene = scene
        self.card = card

    def redo(self):
        self.scene.removeItem(self.card)

    def undo(self):
        self.scene.addItem(self.card)


class CanvasControlPanel(QFrame):
    """Единая вертикальная панель инструментов со строгими квадратными кнопками."""
    def __init__(self, view: 'CanvasView', parent=None, *, show_history: bool = True):
        super().__init__(parent or view)
        self.view = view
        self.setObjectName("CanvasControlPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedWidth(34)

        apply_style(self, '\n            #CanvasControlPanel {\n                background-color: @background.window@;\n                border: 1px solid @background.surface@;\n                border-radius: 6px;\n            }\n            #CanvasControlPanel QToolButton {\n                background-color: transparent;\n                border: none;\n                border-radius: 4px;\n                color: @text.secondary@;\n                font-size: 13px;\n                min-width: 28px;\n                max-width: 28px;\n                min-height: 28px;\n                max-height: 28px;\n                padding: 0px;\n            }\n            #CanvasControlPanel QToolButton:hover {\n                background-color: @control.hover@;\n                color: @text.primary@;\n            }\n            #CanvasControlPanel QToolButton:pressed {\n                background-color: @background.panel@;\n            }\n            #CanvasControlPanel QToolButton:disabled {\n                color: @border.subtle@;\n            }\n        ')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 3, 2, 3)
        layout.setSpacing(2)
        layout.setAlignment(Qt.AlignHCenter)

        # 1. Настройки
        self.btn_settings = QToolButton(self)
        symbol_button(self.btn_settings, "⚙")
        set_text(self.btn_settings, 'ui.canvas_view.board_settings_61a9', setter='setToolTip')
        self.btn_settings.setFixedSize(28, 28)
        self.btn_settings.setCursor(Qt.PointingHandCursor)
        self.btn_settings.clicked.connect(self._show_settings_menu)
        layout.addWidget(self.btn_settings)

        # 2. Приблизить (+)
        self.btn_zoom_in = QToolButton(self)
        symbol_button(self.btn_zoom_in, "+")
        set_text(self.btn_zoom_in, 'ui.canvas_view.zoom_in_bfb8', setter='setToolTip')
        self.btn_zoom_in.setFixedSize(28, 28)
        self.btn_zoom_in.setCursor(Qt.PointingHandCursor)
        self.btn_zoom_in.clicked.connect(self.view.zoom_in)
        layout.addWidget(self.btn_zoom_in)

        # 3. Сброс масштаба и возврат в центр
        self.btn_reset = QToolButton(self)
        set_icon(self.btn_reset, 'refresh', square=True)
        set_text(self.btn_reset, 'ui.canvas_view.center_view_at_100_6ac8', setter='setToolTip')
        self.btn_reset.setFixedSize(28, 28)
        self.btn_reset.setCursor(Qt.PointingHandCursor)
        self.btn_reset.clicked.connect(self.view.reset_view)
        layout.addWidget(self.btn_reset)

        # 4. Отдалить (-)
        self.btn_zoom_out = QToolButton(self)
        symbol_button(self.btn_zoom_out, "−")
        set_text(self.btn_zoom_out, 'ui.canvas_view.zoom_out_8e6a', setter='setToolTip')
        self.btn_zoom_out.setFixedSize(28, 28)
        self.btn_zoom_out.setCursor(Qt.PointingHandCursor)
        self.btn_zoom_out.clicked.connect(self.view.zoom_out)
        layout.addWidget(self.btn_zoom_out)

        self.btn_undo = None
        self.btn_redo = None
        if show_history and hasattr(self.view, "undo_stack"):
            # 5. Отмена действия (Undo)
            self.btn_undo = QToolButton(self)
            symbol_button(self.btn_undo, "↶")
            set_text(self.btn_undo, 'ui.canvas_view.undo_ctrl_z_13d8', setter='setToolTip')
            self.btn_undo.setFixedSize(28, 28)
            self.btn_undo.setCursor(Qt.PointingHandCursor)
            self.btn_undo.clicked.connect(self.view._handle_undo)
            layout.addWidget(self.btn_undo)

            # 6. Повтор действия (Redo)
            self.btn_redo = QToolButton(self)
            symbol_button(self.btn_redo, "↷")
            set_text(self.btn_redo, 'ui.canvas_view.redo_adb0', setter='setToolTip')
            self.btn_redo.setFixedSize(28, 28)
            self.btn_redo.setCursor(Qt.PointingHandCursor)
            self.btn_redo.clicked.connect(self.view._handle_redo)
            layout.addWidget(self.btn_redo)

            self.view.undo_stack.canUndoChanged.connect(self.btn_undo.setEnabled)
            self.view.undo_stack.canRedoChanged.connect(self.btn_redo.setEnabled)
            self.btn_undo.setEnabled(self.view.undo_stack.canUndo())
            self.btn_redo.setEnabled(self.view.undo_stack.canRedo())

    def _show_settings_menu(self):
        menu = CanvasPersistentMenu(self)
        menu.setObjectName("CanvasSettingsMenu")

        custom_populator = getattr(self.view, "populate_settings_menu", None)
        if callable(custom_populator):
            custom_populator(menu)
        else:
            act_snap = localize(add_caption_action(menu, 'addAction', "🧲 Привязка к сетке"), 'ui.canvas_view.snap_to_grid_c296')
            act_snap.setCheckable(True)
            act_snap.setChecked(self.view.snap_to_grid)
            act_snap.triggered.connect(self.view.set_snap_to_grid)

            act_align = localize(add_caption_action(menu, 'addAction', "📐 Выравнивание объектов"), 'ui.canvas_view.align_objects_b666')
            act_align.setCheckable(True)
            act_align.setChecked(self.view.smart_alignment)
            act_align.triggered.connect(lambda ch: (setattr(self.view, "smart_alignment", ch), setattr(self.view.canvas_scene, "smart_alignment", ch)))

            act_raise = localize(add_caption_action(menu, 'addAction', 'Поднимать выделенные карточки'), 'ui.canvas_view.raise_selected_cards_f4f1')
            act_raise.setCheckable(True)
            act_raise.setChecked(performance_preferences.values['board_cards'].get('raise_selected', True))
            act_raise.triggered.connect(lambda enabled: performance_preferences.update('board_cards', 'raise_selected', enabled))

            act_readonly = localize(add_caption_action(menu, 'addAction', "🔒 Режим только чтение"), 'ui.canvas_view.read_only_mode_2070')
            act_readonly.setCheckable(True)
            act_readonly.setChecked(self.view.read_only)
            act_readonly.triggered.connect(self.view.set_read_only)

            menu.addSeparator()
            size_widget = QWidget(menu)
            size_layout = QFormLayout(size_widget)
            size_layout.addRow(localize(QLabel(''), 'ui.canvas_view.new_card_size_grid_steps_2e58'))
            for key, label in (('width', tr('ui.toolbox.width_3019')), ('height', tr('ui.caption.cc8385071149'))):
                control = QSpinBox(size_widget)
                control.setObjectName('BoardDefaultCard_' + key)
                control.setRange(2, 200)
                control.setValue(performance_preferences.values['board_cards'][key])
                control.valueChanged.connect(lambda value, name=key: performance_preferences.update('board_cards', name, value))
                size_layout.addRow(label, control)
            size_action = QWidgetAction(menu)
            size_action.setDefaultWidget(size_widget)
            add_caption_action(menu, 'addAction', size_action)

        menu.exec(self.btn_settings.mapToGlobal(QPoint(self.btn_settings.width() + 4, 0)))


class CanvasScene(QGraphicsScene):
    def _schedule_raise_selected(self):
        from shiboken6 import isValid
        if not isValid(self) or not isValid(self._raise_timer): return
        if not self._raise_pending:
            self._raise_pending = True
            self._raise_timer.start(0)

    def _raise_selected(self):
        from shiboken6 import isValid
        if not isValid(self): return
        self._raise_pending = False
        if self.read_only or not performance_preferences.values['board_cards'].get('raise_selected', True):
            return
        views = self.views()
        if not views or not hasattr(views[0], 'undo_stack'):
            return
        cards = sorted((card for card in self.items() if isinstance(card, CanvasCardItem)),
                       key=lambda card: card.zValue())
        selected = [card for card in cards if card.isSelected()]
        if not selected:
            return
        ordered = [card for card in cards if not card.isSelected()] + selected
        if cards == ordered:
            return
        changes = [(card, card.zValue(), float(index)) for index, card in enumerate(ordered)]
        views[0].undo_stack.push(ChangeZOrderCommand(changes, self))

    def __init__(self, parent=None):
        super().__init__(parent)
        theme_manager.theme_changed.connect(self._refresh_theme)
        self.setSceneRect(-100000, -100000, 200000, 200000)
        self.smart_alignment = True
        self.read_only = False
        self._raise_pending = False
        self._raise_timer = QTimer(self)
        self._raise_timer.setSingleShot(True)
        self._raise_timer.timeout.connect(self._raise_selected)
        self.selectionChanged.connect(self._schedule_raise_selected)
        self.active_guides: List[QLineF] = []
        self._cards = {}
        self._arrows = {}
        self._next_card_order = 0
        self._ordered_cards = None
        self._alignment_dirty = set()
        self._alignment_values = {}
        self._x_entries = []
        self._y_entries = []
        self._active_previews = set()
        self._preview_target = set()
        self._pending_previews = deque()
        self._active_overviews = set()
        self._pending_overviews = deque()
        self._group_rect = QRectF()
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(16)
        self._preview_timer.timeout.connect(self.refresh_visible_previews)
        self._preview_build_timer = QTimer(self)
        self._preview_build_timer.setSingleShot(True)
        self._preview_build_timer.setInterval(8)
        self._preview_build_timer.timeout.connect(self._activate_previews)
        self._overview_timer = QTimer(self)
        self._overview_timer.setSingleShot(True)
        self._overview_timer.setInterval(16)
        self._overview_timer.timeout.connect(self._build_overviews)
        performance_preferences.changed.connect(self.schedule_previews)
        self.changed.connect(self.schedule_previews)

    def addItem(self, item):
        previous = item.scene()
        if previous is self:
            return
        if isinstance(previous, CanvasScene):
            previous.removeItem(item)
        super().addItem(item)
        if isinstance(item, CanvasCardItem):
            self._cards[item] = self._next_card_order
            self._next_card_order += 1
            self._alignment_dirty.add(item)
            self.card_order_changed()
        elif isinstance(item, CanvasArrowItem):
            self._arrows[item] = None
        self.schedule_previews()

    def removeItem(self, item):
        if isinstance(item, CanvasCardItem):
            self._remove_alignment_entries(item)
            self._alignment_dirty.discard(item)
            self._active_previews.discard(item)
            self._preview_target.discard(item)
            self._active_overviews.discard(item)
            self._pending_overviews = deque(card for card in self._pending_overviews if card is not item)
            item.set_overview_active(False)
            self._pending_previews = deque(card for card in self._pending_previews if card is not item)
            item.set_preview_active(False)
            self._cards.pop(item, None)
            self.card_order_changed()
        elif isinstance(item, CanvasArrowItem):
            self._arrows.pop(item, None)
        super().removeItem(item)

    def clear(self):
        self._preview_timer.stop()
        self._preview_build_timer.stop()
        self._overview_timer.stop()
        self._cards.clear()
        self._arrows.clear()
        self._active_previews.clear()
        self._preview_target.clear()
        self._pending_previews.clear()
        self._active_overviews.clear()
        self._pending_overviews.clear()
        self._alignment_dirty.clear()
        self._alignment_values.clear()
        self._x_entries.clear()
        self._y_entries.clear()
        self._ordered_cards = None
        self._group_rect = QRectF()
        super().clear()

    def card_order_changed(self):
        self._ordered_cards = None

    def ordered_cards(self):
        if self._ordered_cards is None:
            self._ordered_cards = sorted(self._cards, key=lambda c: (c.zValue(), self._cards[c]), reverse=True)
        return self._ordered_cards

    def card_geometry_changed(self, card):
        if card in self._cards:
            self._alignment_dirty.add(card)
        self.schedule_previews()

    def _remove_alignment_entries(self, card):
        old = self._alignment_values.pop(card, None)
        if old is None:
            return
        for entries, values in zip((self._x_entries, self._y_entries), old):
            for entry in values:
                index = bisect_left(entries, entry)
                if index < len(entries) and entries[index] == entry:
                    entries.pop(index)

    def alignment_candidates(self, xs, ys, threshold):
        viewports = [view.mapToScene(view.viewport().rect()) for view in self.views()
                     if view.isVisible()]
        if not viewports:
            return []
        for card in self._alignment_dirty:
            self._remove_alignment_entries(card)
            order = self._cards[card]
            x, y = card.pos().x(), card.pos().y()
            values = ([(v, order, card) for v in (x, x + card._width / 2, x + card._width)],
                      [(v, order, card) for v in (y, y + card._height / 2, y + card._height)])
            self._alignment_values[card] = values
            for entries, axis in zip((self._x_entries, self._y_entries), values):
                for entry in axis:
                    insort(entries, entry)
        self._alignment_dirty.clear()
        candidates = set()
        for entries, coords in ((self._x_entries, xs), (self._y_entries, ys)):
            for coordinate in coords:
                start = bisect_left(entries, (coordinate - threshold, -1))
                end = bisect_right(entries, (coordinate + threshold, self._next_card_order))
                candidates.update(entry[2] for entry in entries[start:end])
        candidates = [card for card in candidates if card.isVisible() and any(
            viewport.intersects(card.mapToScene(QRectF(0, 0, card._width, card._height)))
            for viewport in viewports)]
        return sorted(candidates, key=lambda c: (c.zValue(), self._cards[c]), reverse=True)

    def schedule_previews(self, *_):
        if not self._preview_timer.isActive():
            self._preview_timer.start()

    def refresh_visible_previews(self):
        self._preview_timer.stop()
        visible = set()
        overviews = set()
        options = performance_preferences.values['board_rendering']
        for view in self.views():
            detailed = not options['enabled'] or abs(view.transform().m11()) + 1e-9 >= options['threshold'] / 100
            if view.isVisible():
                rect = view.mapToScene(view.viewport().rect()).boundingRect().adjusted(-80, -80, 80, 80)
                target = visible if detailed else overviews
                target.update(item for item in self.items(rect, Qt.IntersectsItemBoundingRect)
                              if isinstance(item, CanvasCardItem))
        for card in self._active_overviews - overviews:
            card.set_overview_active(False)
        pending = [card for card in self._pending_overviews if card in overviews]
        queued = set(pending)
        for card in overviews:
            card.set_overview_active(True)
            if (card not in self._active_overviews or card._overview_key is None) and card not in queued:
                pending.append(card)
        self._active_overviews = overviews
        self._pending_overviews = deque(pending)
        if pending and not self._overview_timer.isActive():
            self._overview_timer.start()
        for card in self._active_previews - visible:
            card.set_preview_active(False)
        self._active_previews.intersection_update(visible)
        if visible != self._preview_target:
            self._preview_target = visible
            # Keep established previews; only newly exposed content needs work.
            # Build expensive Markdown widgets in bounded event-loop slices.
            self._pending_previews = deque(sorted(visible - self._active_previews,
                                                  key=lambda card: self._cards[card]))
        if self._pending_previews and not self._preview_build_timer.isActive():
            self._preview_build_timer.start()

    def _activate_previews(self):
        deadline = time.monotonic() + 0.006
        while self._pending_previews:
            card = self._pending_previews.popleft()
            if card not in self._preview_target or card not in self._cards:
                continue
            card.set_preview_active(True)
            self._active_previews.add(card)
            if time.monotonic() >= deadline:
                break
        if self._pending_previews:
            self._preview_build_timer.start()

    def _build_overviews(self):
        deadline = time.monotonic() + .004
        while self._pending_overviews:
            card = self._pending_overviews.popleft()
            if card not in self._active_overviews or card not in self._cards:
                continue
            card.build_overview()
            if time.monotonic() >= deadline:
                break
        if self._pending_overviews:
            self._overview_timer.start()

    @staticmethod
    def _guides_rect(guides):
        rect = QRectF()
        for line in guides:
            rect = rect.united(QRectF(line.p1(), line.p2()).normalized().adjusted(-3, -3, 3, 3))
        return rect

    def set_guides(self, guides: List[QLineF]):
        if guides == self.active_guides:
            return
        dirty = self._guides_rect(self.active_guides).united(self._guides_rect(guides))
        self.active_guides = guides
        if not dirty.isEmpty():
            self.update(dirty)

    def clear_guides(self):
        self.set_guides([])

    def update_group_frame(self, rect):
        if rect == self._group_rect:
            return
        dirty = self._group_rect.united(rect).adjusted(-3, -3, 3, 3)
        self._group_rect = QRectF(rect)
        self.update(dirty)

    def _refresh_theme(self, *_):
        self.invalidate(self.sceneRect(), QGraphicsScene.AllLayers)
        for item in self.items():
            item.update()
            if hasattr(item, 'update_path'):
                item.update_path()
        for view in self.views():
            view.viewport().update()

    def drawForeground(self, painter: QPainter, rect: QRectF):
        super().drawForeground(painter, rect)
        # Отрисовка направляющих выравнивания
        if self.active_guides:
            painter.setRenderHint(QPainter.Antialiasing)
            pen = QPen(theme_manager.color('accent.primary'), 1.2, Qt.DashLine)
            painter.setPen(pen)
            for line in self.active_guides:
                painter.drawLine(line)

        # Отрисовка внешней границы группы выделения, если выбрано более 1 карточки
        selected_cards = [it for it in self.selectedItems() if isinstance(it, CanvasCardItem)]
        if len(selected_cards) > 1:
            union_rect = QRectF()
            for c in selected_cards:
                union_rect = union_rect.united(c.sceneBoundingRect())
            padded = union_rect.adjusted(-10, -10, 10, 10)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(theme_manager.color('accent.primary'), 1.5))
            painter.drawRoundedRect(padded, 8, 8)

        # Draw above both embedded Markdown previews and live text editors.
        # Mouse ownership also covers resizing a card that wasn't selected.
        grabber = self.mouseGrabberItem()
        while grabber is not None and not isinstance(grabber, CanvasCardItem):
            grabber = grabber.parentItem()
        candidates = selected_cards + ([grabber] if grabber is not None and grabber not in selected_cards else [])
        for card in candidates:
            if card._resizing:
                card.paint_resize_rulers(painter)

    def drawBackground(self, painter: QPainter, rect: QRectF):
        painter.fillRect(rect, theme_manager.color('workspace.background'))

        # Масштаб отображения
        scale = painter.transform().m11()
        if scale < 0.05:
            return

        base_step = int(GRID_SIZE)

        # Адаптивный шаг (LOD): при сильном отдалении группируем точки, чтобы не спамить 100k вызовов
        mult = 1
        while (base_step * mult * scale) < 14:
            mult *= 2
        step = base_step * mult

        left = int(math.floor(rect.left() / step)) * step
        top = int(math.floor(rect.top() / step)) * step
        right = int(math.ceil(rect.right() / step)) * step
        bottom = int(math.ceil(rect.bottom() / step)) * step

        dot_size = 2.0 if mult > 1 else 1.6
        painter.setPen(QPen(theme_manager.color('border.subtle'), dot_size))

        # Батчинг точек в один нативный C++ вызов drawPoints
        from PySide6.QtGui import QPolygonF
        points = []
        for x in range(left, right + step, step):
            for y in range(top, bottom + step, step):
                points.append(QPointF(x, y))

        if points:
            painter.drawPoints(QPolygonF(points))

class CanvasView(QGraphicsView):
    """Основной вьюпорт бесконечной доски."""
    modified = Signal()
    camera_changed = Signal()
    agent_request_submitted = Signal(str, object)
    _tip_grab_item_type = CanvasArrowItem
    ENDPOINT_GRAB_RADIUS = 18

    def __init__(self, parent=None):
        super().__init__(parent)
        self.canvas_scene = CanvasScene(self)
        self.setScene(self.canvas_scene)

        self.setRenderHint(QPainter.Antialiasing)
        self.setRenderHint(QPainter.SmoothPixmapTransform)
        # SmartViewportUpdate перерисовывает только изменившиеся участки, снижая нагрузку
        self.setViewportUpdateMode(QGraphicsView.SmartViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setAcceptDrops(True)
        from ui.views.link_cursor import register_link_cursor
        register_link_cursor(self.viewport(), self._link_cursor_at_global)

        self._is_panning = False
        self._pan_start_pos = QPoint()
        self._current_zoom = 1.0
        self._pending_camera = None
        self._camera_ready = False
        self._restoring_camera = False
        self._camera_restore_timer = QTimer(self)
        self._camera_restore_timer.setSingleShot(True)
        self._camera_restore_timer.setInterval(0)
        self._camera_restore_timer.timeout.connect(self._finish_camera_restore)
        self.horizontalScrollBar().valueChanged.connect(self._camera_moved)
        self.verticalScrollBar().valueChanged.connect(self._camera_moved)

        self.snap_to_grid = True
        self.smart_alignment = True
        self.read_only = False

        self.file_path: Optional[str] = None
        self.edges_data: List[dict] = []
        self.undo_stack = QUndoStack(self)
        self.undo_stack.indexChanged.connect(self._on_stack_changed)
        self._clipboard_cards: List[dict] = []

        # Интерактивное протягивание стрелочки
        self._connecting_card = None
        self._connecting_side = None
        self._connection_view_pos = None
        self._temp_line_item = None
        self._connection_hover_card = None
        self._connection_hover_side = None
        self._endpoint_grab = None

        # Agent context is deliberately independent from QGraphicsScene
        # selection.  Qt selection remains short-lived input; these ids are the
        # persistent cards/arrows that will be sent with one Agent request.
        self._agent_context_active = False
        self._agent_context_node_ids: Set[str] = set()
        self._agent_context_explicit_ids: Set[str] = set()
        self._agent_tree_edge_ids: Set[str] = set()
        self._agent_context_edge_ids: Set[str] = set()
        self._agent_pair_connections_enabled = False
        self._agent_tree_mode_enabled = False
        self._agent_consuming_selection = False

        # Overlay workspaces (currently Modules) can suspend board keyboard
        # commands while the Board canvas remains alive underneath them.
        self.keyboard_commands_enabled = True

        # Локальные шорткаты холста (WidgetWithChildrenShortcut предотвращает конфликт с другими окнами)
        self.shortcut_undo = QShortcut(QKeySequence.Undo, self, self._handle_undo, context=Qt.WidgetWithChildrenShortcut)
        self.shortcut_redo = QShortcut(QKeySequence.Redo, self, self._handle_redo, context=Qt.WidgetWithChildrenShortcut)
        self.shortcut_del = QShortcut(QKeySequence.Delete, self, self._delete_selected_cards, context=Qt.WidgetWithChildrenShortcut)
        self.shortcut_copy = QShortcut(QKeySequence.Copy, self, self.copy_selected_cards, context=Qt.WidgetWithChildrenShortcut)
        self.shortcut_paste = QShortcut(QKeySequence.Paste, self, self.paste_cards, context=Qt.WidgetWithChildrenShortcut)

        # Создаем плавающую панель инструментов
        self.control_panel = CanvasControlPanel(self, self)
        self._update_panel_position()

        # Всплывающая панель действий над выделением
        self.selection_toolbar = CanvasSelectionToolbar(self, self)
        self.canvas_scene.selectionChanged.connect(self._update_selection_toolbar)

        # Board -> Agent composer lives over the viewport and stays open while
        # the user keeps selecting cards on the scene.
        self.agent_popup = CanvasAgentPopup(self, self)
        self.agent_popup.submitted.connect(self._submit_agent_popup)
        self.agent_popup.cancelled.connect(self.cancel_agent_context)

        self.search_popup = SearchReplacePopup(self)
        self.search_popup.search_changed.connect(self._on_canvas_search_changed)
        self.search_popup.next_requested.connect(self._canvas_search_next)
        self.search_popup.replace_requested.connect(self._replace_canvas_search_current)
        self.search_popup.replace_all_requested.connect(self._replace_canvas_search_all)
        self.search_popup.closed.connect(self._clear_canvas_search)
        self._canvas_search_matches = []
        self._canvas_search_index = -1
        self.shortcut_find = QShortcut(QKeySequence.Find, self)
        self.shortcut_find.activated.connect(self.open_search_popup)

    def _on_stack_changed(self, idx: int):
        self.modified.emit()

    def set_keyboard_commands_enabled(self, enabled: bool):
        """Enable/disable board editing shortcuts as one keyboard scope."""
        self.keyboard_commands_enabled = bool(enabled)
        for shortcut in (
            self.shortcut_undo,
            self.shortcut_redo,
            self.shortcut_del,
            self.shortcut_copy,
            self.shortcut_paste,
        ):
            shortcut.setEnabled(self.keyboard_commands_enabled)

    def _camera_moved(self, *_):
        zoom = self.transform().m11()
        if zoom != getattr(self, '_arrow_control_zoom', None):
            self._arrow_control_zoom = zoom
            for arrow in self._all_arrows():
                arrow.update_control_scale(zoom)
        self.canvas_scene.schedule_previews()
        if self._camera_ready and not self._restoring_camera and self.isVisible():
            self.camera_changed.emit()

    def _restore_camera(self):
        if self._pending_camera is None or not self.isVisible():
            return
        center, zoom = self._pending_camera
        self._restoring_camera = True
        self.resetTransform()
        self.scale(zoom, zoom)
        self._current_zoom = zoom
        self.centerOn(QPointF(*center))
        self._restoring_camera = False

    def _finish_camera_restore(self):
        self._restore_camera()
        if self.isVisible() and self._pending_camera is not None:
            self._pending_camera = None
            self._camera_ready = True
        self.canvas_scene.schedule_previews()

    def hideEvent(self, event):
        if self._pending_camera is None:
            inverse, _ = self.viewportTransform().inverted()
            center = inverse.map(QPointF(self.viewport().width() / 2, self.viewport().height() / 2))
            self._pending_camera = ([center.x(), center.y()], self._current_zoom)
        self.save_camera_state()
        self.canvas_scene.schedule_previews()
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        self.canvas_scene.schedule_previews()
        self._restore_camera()
        self._camera_restore_timer.start()

    def resizeEvent(self, event: QResizeEvent):
        super().resizeEvent(event)
        self._restore_camera()
        self._update_panel_position()
        self._update_selection_toolbar()
        self.canvas_scene.schedule_previews()

    def _update_panel_position(self):
        margin = 16
        self.control_panel.move(
            self.width() - self.control_panel.sizeHint().width() - margin,
            margin
        )
        self.control_panel.raise_()

    # =========================================================================
    # AGENT CONTEXT PICKER
    # =========================================================================

    def _all_cards(self) -> List[CanvasCardItem]:
        return list(self.canvas_scene.ordered_cards())

    def _all_arrows(self) -> List[CanvasArrowItem]:
        return [arrow for _index, arrow in sorted(enumerate(self.canvas_scene._arrows),
                key=lambda pair: (pair[1].zValue(), pair[0]), reverse=True)]

    def has_agent_context(self) -> bool:
        return bool(self._agent_context_node_ids)

    def set_agent_pair_connections_enabled(self, enabled: bool):
        self._agent_pair_connections_enabled = bool(enabled)
        self._recompute_agent_context_edges()

    def set_agent_tree_mode_enabled(self, enabled: bool):
        # This flag only changes the *next selection operation*.  Existing
        # marked cards are not kept as a live closure of a root afterwards.
        self._agent_tree_mode_enabled = bool(enabled)

    def clear_agent_context(self):
        self._agent_context_node_ids.clear()
        self._agent_context_explicit_ids.clear()
        self._agent_tree_edge_ids.clear()
        self._agent_context_edge_ids.clear()
        self._refresh_agent_context_visuals()

    def cancel_agent_context(self):
        self._agent_context_active = False
        self._agent_pair_connections_enabled = False
        self._agent_tree_mode_enabled = False
        self.clear_agent_context()

    def _workspace_relative_canvas_path(self) -> str:
        """User-facing path: <workspace-folder>/<path-inside-workspace>."""
        if not self.file_path:
            return "workspace"
        file_path = Path(self.file_path).resolve()
        workspace = Path(app_state.view_workspace("board")).resolve()
        try:
            rel = file_path.relative_to(workspace).as_posix()
        except ValueError:
            rel = Path(os.path.relpath(file_path, workspace)).as_posix()
        workspace_name = workspace.name or "workspace"
        return f"{workspace_name}/{rel.lstrip('/')}"

    def _downstream_subgraph(self, roots: Set[str]) -> Tuple[Set[str], Set[str]]:
        """Return nodes/edges reachable strictly in arrow direction."""
        adjacency: Dict[str, List[CanvasArrowItem]] = {}
        for arrow in self._all_arrows():
            adjacency.setdefault(arrow.from_card.node_id, []).append(arrow)

        visited: Set[str] = set(roots)
        edge_ids: Set[str] = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            for arrow in adjacency.get(current, []):
                edge_ids.add(arrow.edge_id)
                nxt = arrow.to_card.node_id
                if nxt not in visited:
                    visited.add(nxt)
                    stack.append(nxt)
        return visited, edge_ids

    def _apply_agent_card_selection(self, cards: List[CanvasCardItem]):
        if not cards:
            return

        roots = {card.node_id for card in cards}
        toggled_nodes = set(roots)
        toggled_tree_edges: Set[str] = set()
        if self._agent_tree_mode_enabled:
            toggled_nodes, toggled_tree_edges = self._downstream_subgraph(roots)

        if self._agent_tree_mode_enabled:
            # Tree mode is an *expand/add* operation.  A root that was already
            # marked must stay marked when the user asks to include its
            # downstream graph; descendants/edges already present also stay
            # present.  Manual selection without tree mode remains XOR and can
            # still be used to remove individual cards afterwards.
            self._agent_context_node_ids.update(toggled_nodes)
            self._agent_context_explicit_ids.update(roots)
            self._agent_tree_edge_ids.update(toggled_tree_edges)
        else:
            # Ordinary selection keeps the familiar toggle/XOR behaviour.
            for node_id in toggled_nodes:
                if node_id in self._agent_context_node_ids:
                    self._agent_context_node_ids.remove(node_id)
                else:
                    self._agent_context_node_ids.add(node_id)

            for root_id in roots:
                if root_id in self._agent_context_node_ids:
                    self._agent_context_explicit_ids.add(root_id)
                else:
                    self._agent_context_explicit_ids.discard(root_id)

        # Never retain a tree edge whose endpoint was manually removed.  This
        # keeps a later re-add of that card from resurrecting an old edge.
        valid_tree_edges: Set[str] = set()
        for arrow in self._all_arrows():
            if (
                arrow.edge_id in self._agent_tree_edge_ids
                and arrow.from_card.node_id in self._agent_context_node_ids
                and arrow.to_card.node_id in self._agent_context_node_ids
            ):
                valid_tree_edges.add(arrow.edge_id)
        self._agent_tree_edge_ids = valid_tree_edges

        self._agent_context_explicit_ids.intersection_update(self._agent_context_node_ids)
        self._recompute_agent_context_edges()

    def _recompute_agent_context_edges(self):
        edge_ids = set(self._agent_tree_edge_ids)
        if self._agent_pair_connections_enabled:
            for arrow in self._all_arrows():
                if (
                    arrow.from_card.node_id in self._agent_context_node_ids
                    and arrow.to_card.node_id in self._agent_context_node_ids
                ):
                    edge_ids.add(arrow.edge_id)
        self._agent_context_edge_ids = edge_ids
        self._refresh_agent_context_visuals()

    def _refresh_agent_context_visuals(self):
        for card in self._all_cards():
            card.set_agent_context_marked(card.node_id in self._agent_context_node_ids)
        for arrow in self._all_arrows():
            arrow.set_agent_context_marked(arrow.edge_id in self._agent_context_edge_ids)
        self.canvas_scene.update()

    def _consume_agent_scene_selection(self):
        if not self._agent_context_active or self._agent_consuming_selection:
            return
        selected = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected:
            return

        self._agent_consuming_selection = True
        try:
            self._apply_agent_card_selection(selected)
            self.canvas_scene.clearSelection()
        finally:
            self._agent_consuming_selection = False

    def _agent_popup_anchor_from_toolbar(self) -> QPoint:
        global_anchor = self.selection_toolbar.mapToGlobal(
            QPoint(self.selection_toolbar.width() // 2, self.selection_toolbar.height())
        )
        return self.viewport().mapFromGlobal(global_anchor)

    def open_agent_from_selection(self):
        selected = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        anchor = self._agent_popup_anchor_from_toolbar()
        self._agent_context_active = True
        if selected:
            self._apply_agent_card_selection(selected)
            self._agent_consuming_selection = True
            try:
                self.canvas_scene.clearSelection()
            finally:
                self._agent_consuming_selection = False
        self.agent_popup.open_at(anchor)

    def open_agent_at(self, viewport_pos: QPoint):
        self._agent_context_active = True
        self.agent_popup.open_at(viewport_pos)

    def build_agent_context_payload(self) -> Dict[str, Any]:
        cards = [c for c in self._all_cards() if c.node_id in self._agent_context_node_ids]
        cards.sort(key=lambda c: (c.pos().y(), c.pos().x(), c.node_id))
        node_ids = {c.node_id for c in cards}

        arrows = [
            a for a in self._all_arrows()
            if a.edge_id in self._agent_context_edge_ids
            and a.from_card.node_id in node_ids
            and a.to_card.node_id in node_ids
        ]
        arrows.sort(key=lambda a: (a.from_card.node_id, a.to_card.node_id, a.edge_id))

        return {
            "schema": "toshy.canvas-context.v1",
            # UI-only origin for the saved preview; never sent to the model.
            "canvasPath": self._workspace_relative_canvas_path(),
            "explicitNodes": [c.node_id for c in cards if c.node_id in self._agent_context_explicit_ids],
            "nodes": [c.to_dict() for c in cards],
            "edges": [a.to_dict() for a in arrows],
        }

    def _submit_agent_popup(self, prompt: str):
        payload = self.build_agent_context_payload()
        clean_prompt = str(prompt or "").strip()
        if not clean_prompt and not payload.get("nodes"):
            return

        # Match the text editor: the compact composer itself contains only the
        # user's prompt. The file path stays in UI metadata only.
        message_parts: List[str] = []
        if clean_prompt:
            message_parts.append(clean_prompt)
        message = "\n\n".join(message_parts)

        self.agent_request_submitted.emit(message, payload)
        self.agent_popup.reset_and_hide()
        self.cancel_agent_context()

    def start_connection(self, card, side: str, scene_pos: QPointF):
        self._connecting_card = card
        self._connecting_side = side
        self._connection_view_pos = self.mapFromScene(scene_pos)
        # The view owns this drag. A lingering scene mouse grab would deliver
        # synthetic moves to the movable source card when zoom scrolls the view.
        grabber = self.canvas_scene.mouseGrabberItem()
        if grabber is not None:
            grabber.ungrabMouse()
        from PySide6.QtWidgets import QGraphicsLineItem
        self._temp_line_item = QGraphicsLineItem()
        self._temp_line_item.setPen(QPen(theme_manager.color('accent.primary'), 1.8, Qt.DashLine))
        self._temp_line_item.setZValue(10)
        self.canvas_scene.addItem(self._temp_line_item)
        start_pt = card.get_port_scene_pos(side)
        self._temp_line_item.setLine(start_pt.x(), start_pt.y(), scene_pos.x(), scene_pos.y())

    def _cancel_connection_drag(self):
        self._clear_connection_target()
        if self._temp_line_item is not None:
            try:
                self.canvas_scene.removeItem(self._temp_line_item)
            except RuntimeError:
                pass
            self._temp_line_item = None
        self._connecting_card = None
        self._connecting_side = None
        self._connection_view_pos = None

    def _update_connection_line(self, view_pos):
        self._connection_view_pos = view_pos
        start = self._connecting_card.get_port_scene_pos(self._connecting_side)
        end = self.mapToScene(view_pos)
        target, side = self._update_connection_target(end)
        if target is not None and side:
            end = target.get_port_scene_pos(side)
        self._temp_line_item.setLine(start.x(), start.y(), end.x(), end.y())

    def _clear_connection_target(self):
        if self._connection_hover_card is not None:
            try:
                self._connection_hover_card.set_connection_target(False)
            except RuntimeError:
                pass
        self._connection_hover_card = None
        self._connection_hover_side = None

    def _connection_target_at(self, scene_pos: QPointF, halo: float = 12.0):
        # halo is in screen pixels. Ignore the item's enlarged paint bounds
        # used for resize handles, and verify actual distance to the card.
        halo = halo / max(0.0001, abs(self.transform().m11()))
        best = None
        best_dist = float("inf")
        region = QRectF(scene_pos.x() - halo, scene_pos.y() - halo, halo * 2, halo * 2)
        for card in self.canvas_scene.items(region, Qt.IntersectsItemBoundingRect):
            if not isinstance(card, CanvasCardItem) or card is self._connecting_card:
                continue
            rect = card.mapRectToScene(QRectF(0, 0, card._width, card._height)).adjusted(-halo, -halo, halo, halo)
            if not rect.contains(scene_pos):
                continue
            local = card.mapFromScene(scene_pos)
            cx = min(max(local.x(), 0.0), card._width)
            cy = min(max(local.y(), 0.0), card._height)
            dist = math.hypot(local.x() - cx, local.y() - cy)
            if dist > halo:
                continue
            if dist < best_dist:
                best_dist = dist
                best = card
        if best is None:
            return None, None
        return best, best.get_closest_port(scene_pos)

    def _update_connection_target(self, scene_pos: QPointF):
        card, side = self._connection_target_at(scene_pos)
        if card is self._connection_hover_card and side == self._connection_hover_side:
            return card, side
        self._clear_connection_target()
        self._connection_hover_card, self._connection_hover_side = card, side
        if card is not None:
            card.set_connection_target(True, side)
        return card, side

    def open_search_popup(self, pos: Optional[QPoint] = None):
        if pos is None:
            pos = QPoint(max(8, self.width() - 470), 34)
        self.search_popup.open_at(pos)

    @staticmethod
    def _search_ranges(text: str, query: str, case_sensitive: bool, whole_word: bool):
        if not query:
            return []
        import re
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.escape(query)
        if whole_word:
            pattern = r"(?<!\w)" + pattern + r"(?!\w)"
        return [(m.start(), m.end()) for m in re.finditer(pattern, text, flags)]

    def _on_canvas_search_changed(self, query: str, case_sensitive: bool, whole_word: bool):
        matches = []
        cards = sorted(self._all_cards(), key=lambda c: (c.pos().y(), c.pos().x(), c.node_id))
        for card in cards:
            for start, end in self._search_ranges(card.editor.toPlainText(), query, case_sensitive, whole_word):
                matches.append((card, start, end))
        self._canvas_search_options = (query, case_sensitive, whole_word)
        self._canvas_search_matches = matches
        self._canvas_search_index = 0 if matches else -1
        self._apply_canvas_search(center=False)

    def _apply_canvas_search(self, *, center: bool = True):
        current_card = None
        if 0 <= self._canvas_search_index < len(self._canvas_search_matches):
            current_card = self._canvas_search_matches[self._canvas_search_index][0]
        cards_with_matches = {card for card, _s, _e in self._canvas_search_matches}
        for card in self._all_cards():
            card.set_search_marked(card in cards_with_matches, card is current_card)
            card.set_search_text(*getattr(self, '_canvas_search_options', ('', False, False)), current=card is current_card)
        self.search_popup.set_status(self._canvas_search_index, len(self._canvas_search_matches))
        if center and current_card is not None:
            self.centerOn(current_card)
            self.canvas_scene.update()

    def _canvas_search_next(self, backwards: bool = False):
        if not self._canvas_search_matches:
            return
        self._canvas_search_index = (self._canvas_search_index + (-1 if backwards else 1)) % len(self._canvas_search_matches)
        self._apply_canvas_search(center=True)

    def _replace_canvas_search_current(self, replacement: str):
        if not self._canvas_search_matches:
            return
        card, start, end = self._canvas_search_matches[self._canvas_search_index]
        from ui.search_highlight import qt_position
        text = card.editor.toPlainText()
        start, end = qt_position(text, start), qt_position(text, end)
        c = card.editor.textCursor(); c.setPosition(start); c.setPosition(end, QTextCursor.KeepAnchor); c.insertText(replacement)
        self._on_canvas_search_changed(self.search_popup.find_edit.text(), self.search_popup.case_btn.isChecked(), self.search_popup.word_btn.isChecked())

    def _replace_canvas_search_all(self, replacement: str):
        if not self._canvas_search_matches:
            return
        by_card = {}
        for card, start, end in self._canvas_search_matches:
            by_card.setdefault(card, []).append((start, end))
        for card, ranges in by_card.items():
            from ui.search_highlight import qt_position
            text = card.editor.toPlainText()
            ranges = [(qt_position(text, start), qt_position(text, end)) for start, end in ranges]
            c = card.editor.textCursor(); c.beginEditBlock()
            try:
                for start, end in reversed(ranges):
                    c.setPosition(start); c.setPosition(end, QTextCursor.KeepAnchor); c.insertText(replacement)
            finally:
                c.endEditBlock()
        self._on_canvas_search_changed(self.search_popup.find_edit.text(), self.search_popup.case_btn.isChecked(), self.search_popup.word_btn.isChecked())

    def _clear_canvas_search(self):
        self._canvas_search_matches = []
        self._canvas_search_index = -1
        for card in self._all_cards():
            card.set_search_marked(False)
            card.set_search_text('')

    def _recover_pointer_state(self):
        """Recover mouse interaction state after a release is lost by Qt."""
        if self._is_panning:
            self._is_panning = False
        if self.cursor().shape() == Qt.ClosedHandCursor:
            self.setCursor(Qt.ArrowCursor)

        for item in self._all_cards():
            if isinstance(item, CanvasCardItem):
                item.recover_pointer_state()
        if hasattr(self.canvas_scene, "clear_guides"):
            self.canvas_scene.clear_guides()

    def copy_selected_cards(self):
        # 1. Если прямо сейчас редактируется текст в карточке — копируем только сырой выделенный текст
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem) and it._is_editing:
                it.editor.copy()
                return

        selected = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected:
            return

        selected_ids = {c.node_id for c in selected}
        edges_to_copy = []

        for it in self._all_arrows():
            if isinstance(it, CanvasArrowItem):
                if it.from_card.node_id in selected_ids and it.to_card.node_id in selected_ids:
                    edges_to_copy.append(it.to_dict())

        cards_data = {
            "nodes": [card.to_dict() for card in selected],
            "edges": edges_to_copy
        }
        self._clipboard_cards = cards_data

        # Public text excludes images; private Markdown keeps their references.
        try:
            from PySide6.QtGui import QGuiApplication
            from ui.markdown_clipboard import selection_mime, source_mime, combine_selections, payload
            selections = []
            for card, node in zip(selected, cards_data['nodes']):
                cursor = QTextCursor(card.editor.document())
                cursor.select(QTextCursor.Document)
                mime = (selection_mime(card.editor.markdown_handler, cursor) if card.editor.markdown_handler
                        else source_mime(card.editor.toPlainText(), self.file_path))
                selections.append(mime)
                node['clipboard_markdown'] = payload(mime)
            mime_data = combine_selections(selections)
            json_bytes = json.dumps(cards_data, ensure_ascii=False).encode("utf-8")
            mime_data.setData("application/x-canvas-selection", QByteArray(json_bytes))

            QGuiApplication.clipboard().setMimeData(mime_data)
        except Exception:
            pass

    def _clipboard_has_paste_data(self) -> bool:
        if self.read_only:
            return False
        try:
            from PySide6.QtGui import QGuiApplication
            mime = QGuiApplication.clipboard().mimeData()
            if mime is not None:
                if mime.hasFormat("application/x-canvas-selection"):
                    return True
                if mime.hasImage() or mime.hasUrls():
                    return True
                if mime.hasText() and bool(mime.text().strip()):
                    return True
                if mime.formats():
                    return False
        except Exception:
            pass
        return bool(getattr(self, "_clipboard_cards", {}))

    def paste_cards(self, target_pos: Optional[QPointF] = None):
        if self.read_only:
            return

        # 1. Если прямо сейчас редактируется текст в карточке — вставляем сырой текст туда
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem) and it._is_editing:
                it.editor.paste()
                return

        from ui.views.inline_images import import_images
        from PySide6.QtGui import QGuiApplication
        mime = QGuiApplication.clipboard().mimeData()
        if mime and not mime.hasFormat('application/x-canvas-selection'):
            from ui.markdown_clipboard import editor_source
            source = editor_source(mime, self.file_path)
            if source is not None:
                dest = target_pos or self.mapToScene(self.viewport().rect().center())
                self.create_card(dest.x(), dest.y(), text=source)
                return
            images = import_images(mime, self.file_path)
            if images:
                dest = target_pos or self.mapToScene(self.viewport().rect().center())
                self.create_card(dest.x(), dest.y(), text='\n'.join(images))
                return

        # 2. The OS clipboard is authoritative. Structured Board data lives in
        # our custom MIME type; ordinary external text must replace a previously
        # copied Board selection instead of reusing the stale local cache.
        clipboard_payload = {}
        clipboard_text = ""
        clipboard_was_read = False
        clipboard_has_other_data = False
        try:
            from PySide6.QtGui import QGuiApplication
            mime = QGuiApplication.clipboard().mimeData()
            if mime is not None:
                clipboard_was_read = True
                formats = list(mime.formats())
                if mime.hasFormat("application/x-canvas-selection"):
                    raw = bytes(mime.data("application/x-canvas-selection"))
                    parsed = json.loads(raw.decode("utf-8"))
                    if isinstance(parsed, dict):
                        clipboard_payload = parsed
                elif mime.hasText() and mime.text().strip():
                    clipboard_text = mime.text().strip()
                elif formats:
                    clipboard_has_other_data = True
        except Exception:
            clipboard_was_read = False

        if not clipboard_was_read:
            clipboard_payload = getattr(self, "_clipboard_cards", {})
        elif not clipboard_payload and not clipboard_text and not clipboard_has_other_data:
            clipboard_payload = getattr(self, "_clipboard_cards", {})

        # 3. Plain external text becomes a new card.
        if clipboard_text:
            try:
                from PySide6.QtGui import QCursor
                if not target_pos:
                    vp_pos = self.viewport().mapFromGlobal(QCursor.pos())
                    if self.viewport().rect().contains(vp_pos):
                        target_pos = self.mapToScene(vp_pos)
                dest = target_pos or QPointF(0, 0)
                self.create_card(dest.x(), dest.y(), text=clipboard_text)
            except Exception:
                pass
            return

        if not clipboard_payload or not clipboard_payload.get("nodes"):
            return

        nodes_data = clipboard_payload.get("nodes", [])
        edges_data = clipboard_payload.get("edges", [])
        if not nodes_data:
            return

        min_x = min(c["x"] for c in nodes_data)
        min_y = min(c["y"] for c in nodes_data)

        # 4. Определяем позицию вставки (под курсор, если мышь на доске)
        from PySide6.QtGui import QCursor
        if not target_pos:
            vp_pos = self.viewport().mapFromGlobal(QCursor.pos())
            if self.viewport().rect().contains(vp_pos):
                target_pos = self.mapToScene(vp_pos)

        if target_pos:
            offset_x = target_pos.x() - min_x
            offset_y = target_pos.y() - min_y
        else:
            from .canvas_card import GRID_SIZE
            offset_x = GRID_SIZE * 2
            offset_y = GRID_SIZE * 2

        if self.snap_to_grid:
            from .canvas_card import GRID_SIZE
            offset_x = round(offset_x / GRID_SIZE) * GRID_SIZE
            offset_y = round(offset_y / GRID_SIZE) * GRID_SIZE

        self.canvas_scene.clearSelection()

        id_map = {}
        new_cards_by_id = {}

        # 5. Групповая вставка через UndoStack
        self.undo_stack.beginMacro("Вставить карточки")
        try:
            for card_data in nodes_data:
                import uuid
                new_id = uuid.uuid4().hex[:16]
                id_map[card_data["id"]] = new_id

                new_data = dict(card_data)
                private = new_data.pop('clipboard_markdown', None)
                if isinstance(private, dict):
                    from ui.markdown_clipboard import MIME, editor_source
                    mime = QMimeData()
                    mime.setData(MIME, json.dumps(private, ensure_ascii=False).encode('utf-8'))
                    source = editor_source(mime, self.file_path)
                    if source is not None:new_data['text'] = source
                new_data["id"] = new_id
                new_data["x"] = card_data["x"] + offset_x
                new_data["y"] = card_data["y"] + offset_y

                card = CanvasCardItem.from_dict(new_data)
                card.snap_to_grid = self.snap_to_grid
                card.geometry_changed.connect(self.modified.emit)

                cmd = AddCardCommand(self.canvas_scene, card, new_data.get("text", ""))
                self.undo_stack.push(cmd)
                card.setSelected(True)
                new_cards_by_id[new_id] = card

            for e_data in edges_data:
                from_new_id = id_map.get(e_data.get("fromNode"))
                to_new_id = id_map.get(e_data.get("toNode"))

                if from_new_id in new_cards_by_id and to_new_id in new_cards_by_id:
                    from .canvas_arrow import CanvasArrowItem
                    arrow = CanvasArrowItem(
                        from_card=new_cards_by_id[from_new_id],
                        from_side=e_data.get("fromSide", "right"),
                        to_card=new_cards_by_id[to_new_id],
                        to_side=e_data.get("toSide", "left")
                    )
                    cmd_arrow = AddArrowCommand(self.canvas_scene, arrow)
                    self.undo_stack.push(cmd_arrow)
        finally:
            self.undo_stack.endMacro()

        self.modified.emit()
        self._update_selection_toolbar()

    # =========================================================================
    # МАСШТАБИРОВАНИЕ И НАВИГАЦИЯ
    # =========================================================================

    def zoom_in(self):
        self._apply_zoom(1.15)

    def zoom_out(self):
        self._apply_zoom(1.0 / 1.15)

    def _apply_zoom(self, factor: float, anchor_pos=None):
        previous_zoom = self._current_zoom
        new_zoom = self._current_zoom * factor
        if 0.15 <= new_zoom <= 3.0:
            if anchor_pos is None:
                anchor_pos = self._connection_view_pos if self._connecting_card is not None else self.viewport().rect().center()
            scene_anchor = self.mapToScene(anchor_pos)
            previous_anchor = self.transformationAnchor()
            # Qt's AnchorUnderMouse uses the last move delivered to the base
            # view. Connector dragging intercepts those moves, so that cached
            # anchor can be far away. Preserve the actual cursor point instead.
            self.setTransformationAnchor(QGraphicsView.NoAnchor)
            try:
                self.scale(factor, factor)
                delta = self.mapToScene(anchor_pos) - scene_anchor
                self.translate(delta.x(), delta.y())
            finally:
                self.setTransformationAnchor(previous_anchor)
            self._current_zoom = new_zoom
            for arrow in self._all_arrows():
                arrow.update_control_scale(new_zoom)
            if self._connecting_card is not None and self._temp_line_item is not None:
                self._update_connection_line(self._connection_view_pos)
            self._update_selection_toolbar()
            options = performance_preferences.values['board_rendering']
            threshold = options['threshold'] / 100
            if options['enabled'] and ((previous_zoom + 1e-9 < threshold) != (new_zoom + 1e-9 < threshold)):
                # Hide heavy proxies before Qt paints the first overview frame.
                self.canvas_scene.refresh_visible_previews()
            self._camera_moved()

    def reset_view(self):
        self.resetTransform()
        self._current_zoom = 1.0
        cards = self._all_cards()
        if cards:
            rect = self.canvas_scene.itemsBoundingRect()
            self.centerOn(rect.center())
        else:
            self.centerOn(0, 0)
        self._camera_moved()

    def set_snap_to_grid(self, enabled: bool):
        self.snap_to_grid = enabled
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem):
                it.snap_to_grid = self.snap_to_grid

    # =========================================================================
    # РАБОТА С ФАЙЛАМИ
    # =========================================================================

    def clear_canvas(self):
        if hasattr(self, "agent_popup"):
            self.agent_popup.reset_and_hide()
        self.cancel_agent_context()
        self.canvas_scene.clear()
        self.undo_stack.clear()
        self.edges_data = []

    def load_from_file(self, file_path: str):
        self._load_ok = False
        self._camera_ready = False
        self._pending_camera = None
        self.clear_canvas()
        self.file_path = file_path
        try:
            from ui.startup_data import read_startup_data
            data = read_startup_data(file_path)
            self._file_viewport = data.get('viewport')

            cards_by_id = {}
            for idx, n in enumerate(data.get("nodes", [])):
                if n.get("type") == "text":
                    card = CanvasCardItem.from_dict(n)
                    card.set_document_path(file_path)
                    card.setZValue(float(idx))  # Восстанавливаем сохраненный порядок слоев
                    card.snap_to_grid = self.snap_to_grid
                    card.geometry_changed.connect(self.modified.emit)
                    self.canvas_scene.addItem(card)
                    cards_by_id[card.node_id] = card

            for e in data.get("edges", []):
                from_c = cards_by_id.get(e.get("fromNode"))
                to_c = cards_by_id.get(e.get("toNode"))
                if from_c and to_c:
                    arrow = CanvasArrowItem(
                        from_c, e.get("fromSide", "right"),
                        to_c, e.get("toSide", "left"),
                        edge_id=e.get("id")
                    )
                    self.canvas_scene.addItem(arrow)
            from ui.board_camera import load_camera
            camera = load_camera(file_path) or data.get('viewport') or {}
            center = camera.get('center')
            zoom = camera.get('zoom', 1.0)
            if not (isinstance(center, list) and len(center) == 2 and all(isinstance(x, (int, float)) and math.isfinite(x) for x in center)):
                point = self.canvas_scene.itemsBoundingRect().center() if cards_by_id else QPointF(0, 0)
                center = [point.x(), point.y()]
            if not isinstance(zoom, (int, float)) or not math.isfinite(zoom) or not 0.15 <= zoom <= 3.0:
                zoom = 1.0
            self._pending_camera = (center, zoom)
            self._restore_camera()
            self._camera_restore_timer.start()
            self.undo_stack.setClean()
            self._load_ok = True
        except Exception as e:
            print(f"[Canvas] Could not read {file_path}: {e}")

    def save_to_file(self, file_path: Optional[str] = None):
        from core.logger import core_logger
        target_path = file_path or self.file_path
        if not target_path:
            return

        # Сортируем карточки по z-слою (от нижних к верхним), сохраняя порядок наложения
        sorted_cards = sorted(
            self._all_cards(),
            key=lambda c: c.zValue()
        )
        nodes = [c.to_dict() for c in sorted_cards]
        edges = [it.to_dict() for it in self._all_arrows()]

        data = {
            "nodes": nodes,
            "edges": edges,
        }
        # Старая камера в документе остаётся неизменной; текущая хранится в UI.
        if getattr(self,'_file_viewport',None) is not None:
            data['viewport'] = self._file_viewport

        try:
            write_text(target_path, json.dumps(data, ensure_ascii=False, indent=2))
            self.save_camera_state(target_path)
            self.undo_stack.setClean()
            return True
        except Exception as e:
            core_logger.event('storage', 'FILE_SAVE_ERROR', f'{target_path}: {e}', level='ERROR')
            return False

    def save_camera_state(self, file_path=None):
        target = file_path or getattr(self,'file_path',None)
        if not target or not getattr(self,'_load_ok',False):return
        if self._pending_camera is not None:
            center, zoom = self._pending_camera
        else:
            inverse, _ = self.viewportTransform().inverted()
            point = inverse.map(QPointF(self.viewport().width()/2, self.viewport().height()/2))
            center, zoom = [point.x(),point.y()], self._current_zoom
        from ui.board_camera import save_camera
        save_camera(target,center,zoom)

    # =========================================================================
    # СОБЫТИЯ МЫШИ
    # =========================================================================

    def wheelEvent(self, event: QWheelEvent):
        # 1. Блокировка зума на время панорамирования колесиком
        if self._is_panning:
            event.accept()
            return

        # 2. Если курсор находится над карточкой в режиме редактирования — скроллим ее текст напрямую
        item = self.itemAt(event.position().toPoint())
        card = None
        curr = item
        while curr:
            if isinstance(curr, CanvasCardItem):
                card = curr
                break
            curr = curr.parentItem()

        if card and card._is_editing and card.proxy.contains(card.proxy.mapFromScene(self.mapToScene(event.position().toPoint()))):
            vbar = card.editor.verticalScrollBar()
            if vbar and vbar.maximum() > 0:
                delta = event.angleDelta().y()
                steps = delta / 120.0
                step_size = max(1, vbar.singleStep()) * 3
                vbar.setValue(vbar.value() - int(round(steps * step_size)))
            event.accept()
            return

        # 4. Обычный зум камеры доски
        delta = event.angleDelta().y()
        if delta == 0:
            return

        factor = 1.12 if delta > 0 else (1.0 / 1.12)
        if self._connecting_card is not None:
            self._connection_view_pos = event.position().toPoint()
        self._apply_zoom(factor, event.position().toPoint())
        event.accept()

    def _begin_endpoint_grab(self, event):
        if self.read_only or event.button() != Qt.LeftButton:
            return False
        pos = event.position().toPoint()
        radius = self.ENDPOINT_GRAB_RADIUS
        region = self.mapToScene(pos.x() - radius, pos.y() - radius, radius * 2, radius * 2).boundingRect()
        candidates = []
        for item in self.canvas_scene.items(region, Qt.IntersectsItemBoundingRect):
            if not isinstance(item, self._tip_grab_item_type):
                continue
            delta = self.mapFromScene(item.get_tip_scene_pos()) - pos
            distance = math.hypot(delta.x(), delta.y())
            if distance <= radius:
                candidates.append((not item.isSelected(), distance, item))
        if not candidates:
            return False
        candidates.sort(key=lambda entry: entry[:2])
        self._endpoint_grab = (candidates[0][2], pos, self.mapToScene(pos))
        self.setFocus(Qt.MouseFocusReason)
        event.accept()
        return True

    def _reroute_endpoint(self, item, scene_pos):
        self.reassign_arrow(item, scene_pos)

    def _move_endpoint_grab(self, event):
        if self._endpoint_grab is None:
            return False
        item, start, scene_pos = self._endpoint_grab
        if not (event.buttons() & Qt.LeftButton):
            self._endpoint_grab = None
            return False
        if (event.position().toPoint() - start).manhattanLength() < QApplication.startDragDistance():
            event.accept()
            return True
        self._endpoint_grab = None
        if item.scene() is self.canvas_scene:
            self._reroute_endpoint(item, scene_pos)
        return False

    def _finish_endpoint_grab(self, event):
        if self._endpoint_grab is None or event.button() != Qt.LeftButton:
            return False
        item, _, _ = self._endpoint_grab
        self._endpoint_grab = None
        if item.scene() is self.canvas_scene:
            if not (event.modifiers() & Qt.ControlModifier):
                self.canvas_scene.clearSelection()
            item.setSelected(True)
        event.accept()
        return True

    def _link_cursor_at_global(self, position):
        local = self.viewport().mapFromGlobal(position)
        if not self.viewport().rect().contains(local):
            return None
        card = self.itemAt(local)
        while card is not None and not isinstance(card, CanvasCardItem):
            card = card.parentItem()
        if not isinstance(card, CanvasCardItem):
            return None
        if card.preview_proxy.isVisible():
            reference = card.preview.link_at_global(position)
            targets = [card, card.preview_proxy, card.preview.viewport()]
            targets.extend(chunk.viewport() for chunk in card.preview._text_chunks
                           if chunk.viewport().rect().contains(chunk.viewport().mapFromGlobal(position)))
        else:
            handler = card.editor.markdown_handler
            reference = handler.get_url_at_pos(position) if handler else None
            targets = [card, card.proxy, card.editor.viewport()]
        return (reference, targets) if reference else None

    def mousePressEvent(self, event: QMouseEvent):
        # Test endpoints before card hit-testing: a tip may lie on the card.
        # A click only selects; detachment starts after the drag threshold.
        if self._begin_endpoint_grab(event):
            return

        # Панорамирование колесиком
        if event.button() == Qt.MiddleButton:
            self._is_panning = True
            self._pan_start_pos = event.pos()
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._move_endpoint_grab(event):
            return
        if self._connecting_card and self._temp_line_item:
            if not (event.buttons() & Qt.LeftButton):
                self._cancel_connection_drag()
            else:
                self._update_connection_line(event.position().toPoint())
                event.accept()
                return

        if self._is_panning:
            if not (event.buttons() & Qt.MiddleButton):
                self._is_panning = False
                self.setCursor(Qt.ArrowCursor)
            else:
                delta = event.pos() - self._pan_start_pos
                self._pan_start_pos = event.pos()
                self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
                self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
                self._update_selection_toolbar()
                event.accept()
                return

        super().mouseMoveEvent(event)
        self._update_selection_toolbar()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if self._finish_endpoint_grab(event):
            return
        if self._connecting_card and event.button() == Qt.LeftButton:
            end_scene_pt = self.mapToScene(event.position().toPoint())
            target_card = self._connection_hover_card
            target_side = self._connection_hover_side
            if target_card is None:
                target_card, target_side = self._connection_target_at(end_scene_pt)

            if target_card and target_side:
                # Защита от дубликатов стрелок между теми же портами
                already_exists = any(
                    isinstance(it, CanvasArrowItem) and
                    it.from_card == self._connecting_card and
                    it.from_side == self._connecting_side and
                    it.to_card == target_card and
                    it.to_side == target_side
                    for it in self._connecting_card.connected_arrows
                )
                if not already_exists:
                    arrow = CanvasArrowItem(self._connecting_card, self._connecting_side, target_card, target_side)
                    cmd = AddArrowCommand(self.canvas_scene, arrow)
                    self.undo_stack.push(cmd)
                    self.modified.emit()

            self._cancel_connection_drag()
            event.accept()
            return

        if event.button() == Qt.MiddleButton and self._is_panning:
            self._is_panning = False
            self.setCursor(Qt.ArrowCursor)
            event.accept()
            return

        super().mouseReleaseEvent(event)
        # QGraphicsProxyWidget / child editors can occasionally swallow the
        # item-level release.  Reset any stale card drag/resize state here.
        self._recover_pointer_state()
        if self._agent_context_active and event.button() == Qt.LeftButton:
            # Rubber-band selection updates several scene items during the
            # drag. Consume the final selection once, after Qt has completed
            # the release, instead of XOR-ing every intermediate state.
            QTimer.singleShot(0, self._consume_agent_scene_selection)

    def focusInEvent(self, event):
        super().focusInEvent(event)
        if QApplication.mouseButtons() == Qt.NoButton:
            self._recover_pointer_state()

    def dragEnterEvent(self, event):
        from ui.image_mime import can_import
        if not self.read_only and (can_import(event.mimeData()) or event.mimeData().hasUrls()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        from ui.image_mime import can_import
        if not self.read_only and (can_import(event.mimeData()) or event.mimeData().hasUrls()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if self.read_only:
            event.ignore()
            return
        from ui.views.inline_images import import_images
        scene_pos = self.mapToScene(event.position().toPoint())
        target = self.itemAt(event.position().toPoint())
        while target is not None and not isinstance(target, CanvasCardItem):
            target = target.parentItem()
        if isinstance(target, CanvasCardItem):
            target.editor.enable_live_preview()
            global_pos = self.viewport().mapToGlobal(event.position().toPoint())
            local = target.editor.viewport().mapFromGlobal(global_pos)
            if target.editor.markdown_handler.images.insert_mime(event.mimeData(), local):
                event.setDropAction(Qt.CopyAction)
                event.accept()
                return
        from ui.markdown_clipboard import editor_source
        source = editor_source(event.mimeData(), self.file_path)
        if source is not None:
            self.create_card(scene_pos.x(), scene_pos.y(), text=source)
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        images = import_images(event.mimeData(), self.file_path)
        if images:
            self.create_card(scene_pos.x(), scene_pos.y(), text='\n'.join(images))
            event.setDropAction(Qt.CopyAction)
            event.accept()
            return
        super().dropEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and not self.read_only:
            item = self.itemAt(event.pos())
            if item is None:
                scene_pos = self.mapToScene(event.pos())
                self.create_card(scene_pos.x(), scene_pos.y())
                event.accept()
                return
        super().mouseDoubleClickEvent(event)

    # =========================================================================
    # КАРТОЧКИ И КОНТЕКСТНЫЕ МЕНЮ
    # =========================================================================

    def create_card(self, x: float, y: float, text: str = "") -> CanvasCardItem:
        if self.snap_to_grid:
            x = round(x / GRID_SIZE) * GRID_SIZE
            y = round(y / GRID_SIZE) * GRID_SIZE

        from ui.tab_memory import performance_preferences
        size = performance_preferences.values['board_cards']
        card = CanvasCardItem(x, y, size['width'] * GRID_SIZE, size['height'] * GRID_SIZE)
        card.set_document_path(self.file_path)
        card.snap_to_grid = self.snap_to_grid
        card.geometry_changed.connect(self.modified.emit)
        cmd = AddCardCommand(self.canvas_scene, card, text)
        self.undo_stack.push(cmd)
        self.modified.emit()

        self.canvas_scene.clearSelection()
        card.setSelected(True)
        card.start_editing()
        return card

    def delete_card(self, card: CanvasCardItem):
        if self.read_only:
            return

        self.undo_stack.beginMacro("Удалить карточку")
        try:
            for arrow in list(card.connected_arrows):
                cmd_arrow = DeleteArrowCommand(self.canvas_scene, arrow)
                self.undo_stack.push(cmd_arrow)
            cmd = DeleteCardCommand(self.canvas_scene, card)
            self.undo_stack.push(cmd)
        finally:
            self.undo_stack.endMacro()

        self.modified.emit()

    def delete_arrow(self, arrow: CanvasArrowItem):
        if self.read_only or arrow.scene() is not self.canvas_scene:
            return
        cmd = DeleteArrowCommand(self.canvas_scene, arrow)
        self.undo_stack.push(cmd)
        self.modified.emit()

    def reassign_arrow(self, arrow: CanvasArrowItem, scene_pos: QPointF):
        """Detach an arrow and continue dragging from its original source."""
        if self.read_only or arrow.scene() is not self.canvas_scene:
            return
        from_card = arrow.from_card
        from_side = arrow.from_side
        cmd = DeleteArrowCommand(self.canvas_scene, arrow)
        self.undo_stack.push(cmd)
        self.modified.emit()
        self.start_connection(from_card, from_side, scene_pos)

    def zoom_to_card(self, card: CanvasCardItem):
        self.fitInView(card.sceneBoundingRect().adjusted(-60, -60, 60, 60), Qt.KeepAspectRatio)
        self._current_zoom = self.transform().m11()

    def _get_card_under_pos(self, view_pos: QPoint) -> Optional[CanvasCardItem]:
        item = self.itemAt(view_pos)
        while item:
            if isinstance(item, CanvasCardItem):
                return item
            item = item.parentItem()
        return None

    def contextMenuEvent(self, event):
        card = self._get_card_under_pos(event.pos())
        scene_pt = self.mapToScene(event.pos())

        if card:
            if not card.isSelected():
                self.canvas_scene.clearSelection()
                card.setSelected(True)

            menu = QMenu(self)
            menu.setObjectName("CanvasCardContextMenu")
            act_zoom = localize(add_caption_action(menu, 'addAction', "🔍 Приблизить к карточке"), 'ui.canvas_view.zoom_to_card_d99c')
            act_copy = localize(add_caption_action(menu, 'addAction', "📋 Копировать (Ctrl+C)"), 'ui.canvas_view.copy_ctrl_c_9d28')
            menu.addSeparator()
            act_front = localize(add_caption_action(menu, 'addAction', "⤒ Отправить вперед"), 'ui.canvas_view.bring_to_front_cd43')
            act_back = localize(add_caption_action(menu, 'addAction', "⤓ Отправить на фон"), 'ui.canvas_view.send_to_back_37cc')
            menu.addSeparator()
            act_delete = localize(add_caption_action(menu, 'addAction', "🗑️ Удалить (Delete)"), 'ui.canvas_view.delete_delete_bd7b')

            action = standardize_workspace_menu(menu).exec(event.globalPos())
            if action == act_zoom:
                self.zoom_to_card(card)
            elif action == act_copy:
                self.copy_selected_cards()
            elif action == act_front:
                self.bring_to_front()
            elif action == act_back:
                self.send_to_back()
            elif action == act_delete:
                self._delete_selected_cards()
        else:
            menu = CanvasPersistentContextMenu(self)
            menu.setObjectName("CanvasContextMenu")

            agent_btn = menu.add_quick_action('sparkles',tr('ui.canvas_view.ask_the_agent_c4dc'))
            search_btn = menu.add_quick_action('search',tr('ui.canvas_view.search_and_replace_c227'))

            def open_agent_from_empty_space():
                menu.close()
                self._agent_consuming_selection = True
                try:
                    self.canvas_scene.clearSelection()
                finally:
                    self._agent_consuming_selection = False
                QTimer.singleShot(0, lambda p=QPoint(event.pos()): self.open_agent_at(p))

            agent_btn.clicked.connect(open_agent_from_empty_space)

            def open_search_from_empty_space():
                menu.close()
                QTimer.singleShot(0, lambda p=QPoint(event.pos()): self.open_search_popup(p))
            search_btn.clicked.connect(open_search_from_empty_space)

            act_add = localize(add_caption_action(menu, 'addAction', "➕ Добавить карточку"), 'ui.canvas_view.add_card_3c1b')
            act_undo = localize(add_caption_action(menu, 'addAction', "↩️ Откатить (Ctrl+Z)"), 'ui.canvas_view.undo_ctrl_z_a4bf')
            act_paste = localize(add_caption_action(menu, 'addAction', "📋 Вставить (Ctrl+V)"), 'ui.canvas_view.paste_ctrl_v_b09e')

            act_undo.setEnabled(self.undo_stack.canUndo())
            act_paste.setEnabled(self._clipboard_has_paste_data())
            if self.read_only:
                act_add.setEnabled(False)

            menu.addSeparator()

            act_snap = localize(add_caption_action(menu, 'addAction', "🧲 Привязка к сетке"), 'ui.canvas_view.snap_to_grid_c296')
            act_snap.setCheckable(True)
            act_snap.setChecked(self.snap_to_grid)
            act_snap.triggered.connect(self.set_snap_to_grid)

            act_align = localize(add_caption_action(menu, 'addAction', "📐 Выравнивание объектов"), 'ui.canvas_view.align_objects_b666')
            act_align.setCheckable(True)
            act_align.setChecked(self.smart_alignment)
            act_align.triggered.connect(lambda ch: (setattr(self, "smart_alignment", ch), setattr(self.canvas_scene, "smart_alignment", ch)))

            act_readonly = localize(add_caption_action(menu, 'addAction', "🔒 Режим только чтение"), 'ui.canvas_view.read_only_mode_2070')
            act_readonly.setCheckable(True)
            act_readonly.setChecked(self.read_only)
            act_readonly.triggered.connect(self.set_read_only)

            action = standardize_workspace_menu(menu).exec(event.globalPos())
            if action == act_add:
                self.create_card(scene_pt.x(), scene_pt.y())
            elif action == act_undo:
                self.undo_stack.undo()
            elif action == act_paste:
                self.paste_cards(scene_pt)

    def keyPressEvent(self, event: QKeyEvent):
        # QShortcut is not the only board command path: CanvasView also handles
        # the same keys here. When an overlay owns keyboard scope, bypass all
        # board editing commands even if this hidden/underlaid view retained
        # focus from before the overlay was opened.
        if not self.keyboard_commands_enabled:
            super().keyPressEvent(event)
            return

        directions = {Qt.Key_Left: (-1, 0), Qt.Key_Right: (1, 0),
                      Qt.Key_Up: (0, -1), Qt.Key_Down: (0, 1)}
        cards = [card for card in self.canvas_scene.selectedItems() if isinstance(card, CanvasCardItem)]
        if event.key() in directions and cards and not any(card._is_editing for card in cards):
            if not self.read_only:
                dx, dy = directions[event.key()]
                offset = QPointF(dx * GRID_SIZE, dy * GRID_SIZE)
                self.undo_stack.push(MoveCardsCommand(
                    [(card, QPointF(card.pos()), card.pos() + offset) for card in cards], self.canvas_scene))
            event.accept()
            return

        if event.matches(QKeySequence.Undo):
            self._handle_undo()
            event.accept()
            return
        elif event.matches(QKeySequence.Redo):
            self._handle_redo()
            event.accept()
            return
        elif event.matches(QKeySequence.Copy) or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_C):
            self.copy_selected_cards()
            event.accept()
            return
        elif event.matches(QKeySequence.Paste) or (event.modifiers() & Qt.ControlModifier and event.key() == Qt.Key_V):
            self.paste_cards()
            event.accept()
            return
        elif event.key() == Qt.Key_Delete:
            self._delete_selected_cards()
            event.accept()
            return
        super().keyPressEvent(event)

    def _delete_selected_cards(self):
        if self.read_only:
            return

        selected_arrows = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasArrowItem)]
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_arrows and not selected_cards:
            return

        # Все удаленные объекты откатываются вместе за один шаг
        self.undo_stack.beginMacro("Удалить выбранное")
        try:
            for arrow in selected_arrows:
                cmd_arrow = DeleteArrowCommand(self.canvas_scene, arrow)
                self.undo_stack.push(cmd_arrow)

            for card in selected_cards:
                for arrow in list(card.connected_arrows):
                    if arrow not in selected_arrows:
                        cmd_arrow = DeleteArrowCommand(self.canvas_scene, arrow)
                        self.undo_stack.push(cmd_arrow)
                cmd = DeleteCardCommand(self.canvas_scene, card)
                self.undo_stack.push(cmd)
        finally:
            self.undo_stack.endMacro()

        self.modified.emit()


    def set_read_only(self, enabled: bool):
        """Включает/выключает режим только чтение для холста и всех нод."""
        self.read_only = enabled
        self.canvas_scene.read_only = enabled
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem):
                it.setFlag(QGraphicsItem.ItemIsMovable, not enabled)
                if enabled and it._is_editing:
                    it._on_editing_finished()
        self.canvas_scene.update()

    def _update_selection_toolbar(self):
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards or self.read_only:
            self.canvas_scene.update_group_frame(QRectF())
            self.selection_toolbar.hide()
            return

        union_rect = QRectF()
        for c in selected_cards:
            # boundingRect also includes the invisible external resize handles.
            union_rect = union_rect.united(c.mapRectToScene(QRectF(0, 0, c._width, c._height)))

        # Если карточек несколько, учитываем внешний отступ рамки
        if len(selected_cards) > 1:
            union_rect = union_rect.adjusted(-10, -10, 10, 10)

        self.canvas_scene.update_group_frame(union_rect if len(selected_cards) > 1 else QRectF())

        # Центрируем над верхней стороной области
        top_center_scene = QPointF(union_rect.center().x(), union_rect.top())
        vp_pt = self.mapFromScene(top_center_scene)

        tb_size = self.selection_toolbar.sizeHint()
        x = int(vp_pt.x() - tb_size.width() / 2)
        y = int(vp_pt.y() - tb_size.height() - 10)

        # Ограничиваем границами видимой области
        x = max(10, min(x, self.viewport().width() - tb_size.width() - 10))
        y = max(10, y)

        self.selection_toolbar.move(x, y)
        self.selection_toolbar.show()
        self.selection_toolbar.raise_()

    def zoom_to_selection(self):
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards:
            return
        union_rect = QRectF()
        for c in selected_cards:
            union_rect = union_rect.united(c.sceneBoundingRect())
        self.fitInView(union_rect.adjusted(-60, -60, 60, 60), Qt.KeepAspectRatio)
        self._current_zoom = self.transform().m11()
        self._update_selection_toolbar()

    def set_selected_cards_color(self, color_hex: Optional[str]):
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards or self.read_only:
            return
        changes = [(c, c.color, color_hex) for c in selected_cards]
        cmd = ChangeCardsColorCommand(changes, self.canvas_scene)
        self.undo_stack.push(cmd)
        self.modified.emit()

    def set_selected_cards_align(self, align_str: str):
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards or self.read_only:
            return
        changes = [(c, c.text_align, align_str) for c in selected_cards]
        cmd = ChangeCardsAlignCommand(changes, self.canvas_scene)
        self.undo_stack.push(cmd)
        self.modified.emit()

    def _handle_undo(self):
        # 1. Если прямо сейчас редактируется текст в карточке — откатываем текст
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem) and it._is_editing:
                it.editor.undo()
                return

        # 2. Иначе откатываем действия на доске (перемещения, создание, стрелки)
        self.undo_stack.undo()

    def _handle_redo(self):
        for it in self._all_cards():
            if isinstance(it, CanvasCardItem) and it._is_editing:
                it.editor.redo()
                return

        self.undo_stack.redo()

    def bring_to_front(self):
        if self.read_only:
            return
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards:
            return

        all_cards = self._all_cards()
        max_z = max((c.zValue() for c in all_cards), default=0.0)

        changes = []
        curr_z = max_z + 1.0
        for c in selected_cards:
            changes.append((c, c.zValue(), curr_z))
            curr_z += 1.0

        cmd = ChangeZOrderCommand(changes, self.canvas_scene)
        self.undo_stack.push(cmd)
        self.modified.emit()

    def send_to_back(self):
        if self.read_only:
            return
        selected_cards = [it for it in self.canvas_scene.selectedItems() if isinstance(it, CanvasCardItem)]
        if not selected_cards:
            return

        all_cards = self._all_cards()
        other_cards = [c for c in all_cards if c not in selected_cards]
        min_z = min((c.zValue() for c in other_cards), default=1.0)

        # Сдвигаем остальные карточки вверх при необходимости, чтобы z карточек не уходил ниже 0
        needed_shift = max(0.0, float(len(selected_cards)) - min_z)
        changes = []
        if needed_shift > 0:
            for c in other_cards:
                changes.append((c, c.zValue(), c.zValue() + needed_shift))

        curr_z = 0.0
        for c in selected_cards:
            changes.append((c, c.zValue(), curr_z))
            curr_z += 1.0

        cmd = ChangeZOrderCommand(changes, self.canvas_scene)
        self.undo_stack.push(cmd)
        self.modified.emit()

from ui.widgets.workspace_menu import WorkspaceContextMenu


class PersistentMenuMixin:
    def mouseReleaseEvent(self, event):
        action = self.actionAt(event.pos())
        if action and action.isCheckable():
            action.trigger()
            self.update()
            return
        super().mouseReleaseEvent(event)


class CanvasPersistentMenu(PersistentMenuMixin, QMenu):
    """Settings have no reserved quick-action row."""
    def __init__(self, parent=None):
        super().__init__(parent)
        from ui.widgets.workspace_menu import standardize_workspace_menu
        standardize_workspace_menu(self)


class CanvasPersistentContextMenu(PersistentMenuMixin, WorkspaceContextMenu):
    """Workspace actions above a persistent checkable action list."""
    pass


class AddArrowCommand(QUndoCommand):
    def __init__(self, scene, arrow):
        super().__init__("Создать стрелочку")
        self.scene = scene
        self.arrow = arrow

    def redo(self):
        if self.arrow.scene() is not self.scene:
            self.scene.addItem(self.arrow)
            if self.arrow not in self.arrow.from_card.connected_arrows:
                self.arrow.from_card.connected_arrows.append(self.arrow)
            if self.arrow not in self.arrow.to_card.connected_arrows:
                self.arrow.to_card.connected_arrows.append(self.arrow)
            self.arrow.update_path()

    def undo(self):
        self.arrow.detach()
        self.scene.removeItem(self.arrow)


class DeleteArrowCommand(QUndoCommand):
    def __init__(self, scene, arrow):
        super().__init__("Удалить стрелочку")
        self.scene = scene
        self.arrow = arrow

    def redo(self):
        self.arrow.detach()
        self.scene.removeItem(self.arrow)

    def undo(self):
        self.scene.addItem(self.arrow)
        self.arrow.from_card.connected_arrows.append(self.arrow)
        self.arrow.to_card.connected_arrows.append(self.arrow)
        self.arrow.update_path()

class MoveCardsCommand(QUndoCommand):
    """Откат перемещения карточек."""
    def __init__(self, moves: list, scene):
        super().__init__("Переместить карточки")
        self.moves = moves  # [(card, old_pos, new_pos), ...]
        self.scene = scene

    def redo(self):
        from .movement import place_cards
        place_cards(self.scene, ((card, new_pos) for card, old_pos, new_pos in self.moves))
        for card, old_pos, new_pos in self.moves:
            for arrow in card.connected_arrows:
                arrow.update_path()
        self.scene.update()

    def undo(self):
        from .movement import place_cards
        place_cards(self.scene, ((card, old_pos) for card, old_pos, new_pos in self.moves))
        for card, old_pos, new_pos in self.moves:
            for arrow in card.connected_arrows:
                arrow.update_path()
        self.scene.update()


class ResizeCardCommand(QUndoCommand):
    """Откат изменения размера карточки."""
    def __init__(self, card, old_geom: QRectF, new_geom: QRectF, scene):
        super().__init__("Изменить размер карточки")
        self.card = card
        self.old_geom = old_geom
        self.new_geom = new_geom
        self.scene = scene

    def redo(self):
        self.card.prepareGeometryChange()
        self.card.setPos(self.new_geom.topLeft())
        self.card._width = self.new_geom.width()
        self.card._height = self.new_geom.height()
        self.card._update_editor_size()
        for arrow in self.card.connected_arrows:
            arrow.update_path()
        self.card.update()
        self.scene.update()

    def undo(self):
        self.card.prepareGeometryChange()
        self.card.setPos(self.old_geom.topLeft())
        self.card._width = self.old_geom.width()
        self.card._height = self.old_geom.height()
        self.card._update_editor_size()
        for arrow in self.card.connected_arrows:
            arrow.update_path()
        self.card.update()
        self.scene.update()
