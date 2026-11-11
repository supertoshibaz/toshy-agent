"""Compact message presentation; conversation ownership stays in ChatView."""
from ui.i18n import add_caption_action, bind_caption, tr, bind_source, localize
from ui.icons import set_icon

from functools import lru_cache
from weakref import WeakSet

from shiboken6 import isValid
from PySide6.QtCore import QEvent, QObject, QPointF, QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLayout, QMenu, QSizePolicy, QToolButton, QWidget


class MessageHoverTracker(QObject):
    """Resolve missing child Leave events without scanning the chat history.

    Qt may synthesize Enter when a message appears under a stationary cursor,
    then send the first real move only to its window. Observe that transition
    once per application and refresh only the currently visible action rows.
    """
    def __init__(self, app):
        super().__init__(app)
        self.visible = WeakSet()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(0)
        self.timer.timeout.connect(self.refresh)
        app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.visible and event.type() in (QEvent.MouseMove, QEvent.Enter, QEvent.Leave,
                                             QEvent.Wheel, QEvent.Move, QEvent.Resize, QEvent.LayoutRequest):
            self.timer.start()
        return False

    def refresh(self):
        for bar in tuple(self.visible):
            if isValid(bar):
                bar.refresh_visibility()
            else:
                self.visible.discard(bar)


def hover_tracker():
    app = QApplication.instance()
    if not hasattr(app, '_message_hover_tracker'):
        app._message_hover_tracker = MessageHoverTracker(app)
    return app._message_hover_tracker


class UserTurnLayout(QLayout):
    """Right-aligned 75% bubble, including height-for-width text wrapping.

    Keeping the turn itself full width preserves timeline and selection order.
    Never set a fixed bubble width: its old minimum would prevent the column
    from shrinking when the window or sidebars change size.
    """
    def __init__(self, parent):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def hasHeightForWidth(self):
        return True

    def _body_height(self, width):
        if not self._items:
            return 0
        item = self._items[0]
        value = item.heightForWidth(max(1, int(width * .75))) if item.hasHeightForWidth() else -1
        return max(item.minimumSize().height(), value if value >= 0 else item.sizeHint().height())

    def heightForWidth(self, width):
        return self._body_height(width) + (self.spacing() + self._items[1].sizeHint().height() if len(self._items) > 1 else 0) + self.contentsMargins().bottom()

    def sizeHint(self):
        width = max(1, self._items[0].sizeHint().width() * 4 // 3) if self._items else 1
        return QSize(width, self.heightForWidth(width))

    def minimumSize(self):
        # Long code/links must not force a wider scrollable chat column.
        return QSize(0, sum(item.minimumSize().height() for item in self._items) + max(0, len(self._items) - 1) * self.spacing())

    def setGeometry(self, rect):
        super().setGeometry(rect)
        if not self._items:
            return
        width = max(1, int(rect.width() * .75))
        left = rect.x() + rect.width() - width
        height = self._body_height(rect.width())
        self._items[0].setGeometry(QRect(left, rect.y(), width, height))
        if len(self._items) > 1:
            self._items[1].setGeometry(QRect(left, rect.y() + height + self.spacing(), width, self._items[1].sizeHint().height()))




class MessageActions(QWidget):
    """A stable footer whose buttons appear only while the message is hovered."""
    def __init__(self, turn, kind):
        super().__init__(turn)
        self.turn = turn
        self.kind = kind
        self.setObjectName('MessageActions')
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setFixedHeight(24)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.row = QHBoxLayout(self)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(2)
        self.buttons = {}
        self._copy_menu = None
        self._hover_timer = QTimer(self)
        self._hover_timer.setSingleShot(True)
        self._hover_timer.setInterval(0)
        self._hover_timer.timeout.connect(self.refresh_visibility)
        if kind == 'user':
            self.row.addStretch(1)
        from ui.views.chat_selection import copy_from_menu
        self.add_button('copy', tr('ui.chat_references.copy_text_3cbc'), lambda: copy_from_menu(turn, False))
        self.add_button('copy_markdown', tr('ui.caption.4e3615ffa9af'), lambda: copy_from_menu(turn, True))
        self.add_button('edit', tr('ui.caption.ddd61b334fcd') if kind == 'thought' else tr('ui.caption.c763f84fd960'), turn._toggle_edit_mode)
        if kind != 'thought':
            self.add_button('reply', tr('ui.chat_references.reply_to_a_message_or_selected_86ad'), self.reply)
        self.add_button('forward', tr('ui.caption.e01abd2530d4'), self.forward)
        self.add_button('pin', tr('ui.caption.4087d0c45e73'), self.toggle_pin)
        self.buttons['pin'].setCheckable(True)
        if kind != 'thought':
            self.add_button('branch', tr('ui.chat_view.create_branch_f261'), lambda: turn.create_branch_requested.emit(turn))
        if kind == 'user':
            self.add_button('retry', tr('ui.chat_view.regenerate_1731'), lambda: turn.retry_requested.emit(turn))
        self.add_button('select', tr('ui.chat_references.select_message_e129'), self.select)
        self.add_button('delete', tr('ui.file_manager_widget.delete_permanently_abac'), lambda: turn.delete_requested.emit(turn))
        if kind != 'user':
            self.row.addStretch(1)
        from ui.plugin_host import populate_slot
        self.plugin_widgets = populate_slot('ui.chat.message.actions', self.row, parent=self, data={'turn': turn, 'kind': kind})
        for widget in self.plugin_widgets:
            policy = widget.sizePolicy(); policy.setRetainSizeWhenHidden(True); widget.setSizePolicy(policy)
            widget.hide()
        turn.edit_btn = self.buttons['edit']
        turn.copy_btn = self.buttons['copy']
        # Keep the old anchor for context-menu callers without exposing ⋮.
        turn.menu_btn = turn.copy_btn
        turn.pin_btn = self.buttons['pin']
        if 'reply' in self.buttons:
            turn.reply_btn = self.buttons['reply']
        for widget in (turn, self, getattr(turn, 'bubble', turn)):
            widget.setMouseTracking(True)
            widget.installEventFilter(self)
        turn.destroyed.connect(self.deleteLater)

    def mousePressEvent(self, event):
        event.accept()

    def mouseReleaseEvent(self, event):
        event.accept()

    def mouseDoubleClickEvent(self, event):
        event.accept()

    def wheelEvent(self, event):
        # Only clicks belong to the footer. Let wheel events reach QScrollArea.
        event.ignore()

    def add_button(self, key, tooltip, callback):
        button = QToolButton(self)
        button.setObjectName('MessageActionBtn')
        bind_source(button, tooltip, setter='setToolTip')
        bind_source(button, tooltip, setter='setAccessibleName')
        button.setCursor(Qt.PointingHandCursor)
        button.setFocusPolicy(Qt.NoFocus)
        button.setFixedSize(24, 24)
        set_icon(button, key, size=18)
        button.setIconSize(QSize(18, 18))
        policy = button.sizePolicy(); policy.setRetainSizeWhenHidden(True); button.setSizePolicy(policy)
        button.clicked.connect(callback)
        self.row.addWidget(button)
        self.buttons[key] = button
        button.hide()
        return button

    def refresh_visibility(self, *, hovered=None):
        from ui.views.chat_selection import message_selection_active
        if not isValid(self.turn):
            self.hide()
            hover_tracker().visible.discard(self)
            return
        if hovered is None:
            from ui.views.chat_model_blocks import members
            inside = any(item.isVisible() and item.rect().contains(item.mapFromGlobal(QCursor.pos()))
                         for item in members(self.turn))
            if self.kind == 'user':
                # Include the layout gap between the bubble and its footer.
                # Separate hit rectangles made the buttons disappear on the
                # way from the panel border to the action row.
                area = self.turn.bubble.geometry().united(self.geometry())
                inside = inside and area.contains(self.turn.mapFromGlobal(QCursor.pos()))
            inside_bar = self.isVisible() and self.rect().contains(self.mapFromGlobal(QCursor.pos()))
            hovered = self.turn.isVisible() and (inside or inside_bar)
        hovered = hovered or bool(self._copy_menu and self._copy_menu.isVisible())
        visible = hovered and not message_selection_active(self.turn)
        for button in [*self.buttons.values(), *self.plugin_widgets]:
            if isValid(button):
                button.setVisible(visible)
        tracker = hover_tracker()
        if visible:
            tracker.visible.add(self)
        else:
            tracker.visible.discard(self)
        from ui.views.chat_references import turn_metadata
        from core.chat_references import PINNED
        pinned = turn_metadata(self.turn)[PINNED]
        bind_caption(self.buttons['pin'], tr('ui.caption.09e33790a780') if pinned else tr('ui.caption.4087d0c45e73'), setter='setToolTip')
        self.buttons['pin'].setChecked(pinned)

    def eventFilter(self, watched, event):
        if event.type() in (QEvent.Enter, QEvent.Leave, QEvent.Show, QEvent.Move, QEvent.Resize):
            # Qt can deliver ancestor/child Leave before updating QCursor.
            # Resolve the final hovered area after the entire transition.
            self._hover_timer.start()
        elif event.type() == QEvent.MouseMove:
            self.refresh_visibility()
        return False

    def show_copy_menu(self):
        from ui.views.chat_selection import copy_from_menu
        menu = QMenu(self)
        menu.setObjectName('ChatMessageContextMenu')
        localize(add_caption_action(menu, 'addAction', 'Копировать как текст'), 'ui.chat_presentation.copy_as_text_6917').triggered.connect(lambda: copy_from_menu(self.turn, False))
        localize(add_caption_action(menu, 'addAction', 'Копировать как Markdown'), 'ui.chat_presentation.copy_as_markdown_2da4').triggered.connect(lambda: copy_from_menu(self.turn, True))
        def closed():
            self._copy_menu = None
            menu.deleteLater()
            from ui.qt_deferred import defer
            defer(0, self, self.refresh_visibility)
        menu.aboutToHide.connect(closed)
        self._copy_menu = menu
        menu.popup(self.buttons['copy'].mapToGlobal(self.buttons['copy'].rect().bottomLeft()))

    def reply(self):
        from ui.views.chat_references import controller_for
        controller = controller_for(self.turn)
        if controller:
            controller.prepare_reply(self.turn)

    def toggle_pin(self):
        from ui.views.chat_references import controller_for
        controller = controller_for(self.turn)
        if controller:
            controller.toggle_pin(self.turn)
        self.refresh_visibility()

    def select(self):
        from ui.views.chat_selection import select_from_menu
        select_from_menu(self.turn)

    def forward(self):
        from ui.views.chat_references import controller_for
        controller = controller_for(self.turn)
        if controller:
            controller.chat.message_selection.forward_turn(self.turn)


def install_actions(turn, kind):
    layout = turn.layout() if kind == 'user' else turn.main_layout
    layout.setSpacing(0)
    turn.actions_bar = MessageActions(turn, kind)
    if kind == 'user':
        turn.layout().addWidget(turn.actions_bar)
    else:
        turn.main_layout.addWidget(turn.actions_bar)


def background_selector(turn):
    return '#' + turn.objectName()


def focus_message_editor(turn):
    """Place the newly collapsed inline editor at the start of its message."""
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QTextCursor
    from ui.qt_deferred import defer
    chat = turn.parentWidget()
    while chat is not None and not (hasattr(chat,'scroll_area') and hasattr(chat,'turns_container')):
        chat = chat.parentWidget()
    if chat is None:
        return
    chat._cancel_history_positioning()
    chat._auto_scroll_to_bottom = False
    chat._thought_scroll_anchor = None
    turn.editor_text.moveCursor(QTextCursor.Start)
    turn.editor_text.setFocus()
    def align():
        if not turn.is_editing:
            return
        turn.main_layout.activate()
        chat.turns_container.layout().activate()
        top = turn.mapTo(chat.turns_container,QPoint()).y()
        chat.scroll_area.verticalScrollBar().setValue(max(0,top-8))
    defer(0,turn,align)
    defer(25,turn,align)
