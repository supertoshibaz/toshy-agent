"""Message selection, clipboard export, and core-owned history moves."""
from ui.themes.manager import theme_manager
from ui.i18n import bind_caption, tr, set_text
from ui.styles.binding import apply_style

import json
from bisect import bisect_left, bisect_right

from shiboken6 import isValid
from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QPainter, QPen, QTextDocument
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QLabel, QToolButton, QWidget, QSizePolicy, QVBoxLayout

from ui.event_bus import event_bus
from ui.app_state import app_state
from ui.widgets import inline_editor as inline


_FILTER_EVENTS = frozenset((QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.Resize,
    QEvent.Move, QEvent.Show, QEvent.Hide, QEvent.LayoutRequest, QEvent.ChildAdded,
    QEvent.ChildRemoved, QEvent.DeferredDelete, QEvent.MouseButtonPress))


class ChatTurnsLayout(QVBoxLayout):
    """Version the message order independently from Qt's layout/paint events.

    All chat history mutations use these layout methods, including retries,
    restored sessions, selection moves and service labels. Geometry invalidation
    is cheap; consumers rebuild their cache only when they actually need it.
    """
    def __init__(self, parent=None):
        self.structure_revision = 0
        self.geometry_revision = 0
        super().__init__(parent)

    def invalidate(self):
        self.geometry_revision += 1
        super().invalidate()

    def addWidget(self, *args):
        super().addWidget(*args)
        self.structure_revision += 1

    def insertWidget(self, *args):
        super().insertWidget(*args)
        self.structure_revision += 1

    def removeWidget(self, widget):
        super().removeWidget(widget)
        self.structure_revision += 1

    def takeAt(self, index):
        item = super().takeAt(index)
        if item is not None:
            self.structure_revision += 1
        return item


def select_from_menu(turn):
    parent = turn.parentWidget()
    while parent is not None:
        controller = getattr(parent, 'message_selection', None)
        if controller is not None:
            controller.begin(turn)
            return
        parent = parent.parentWidget()


def message_selection_active(turn):
    parent = turn.parentWidget()
    while parent is not None:
        controller = getattr(parent, 'message_selection', None)
        if controller is not None:
            return controller.active
        parent = parent.parentWidget()
    return False


def message_markdown(turn):
    from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn
    if isinstance(turn, UserMessageTurn):
        text = turn.raw_text
        files = ['{}. {}'.format(i, a.get('filename') or 'file') for i, a in enumerate(turn.attachments, 1)]
        if files:
            text += tr('ui.caption.fbfbf00e8699') + '\n'.join(files)
        return tr('ui.caption.041bfceecc8f'), text
    if getattr(turn,'is_subagent_block',False):
        return tr('ui.subagents_panel.subagents_cf58'), '\n\n'.join('**'+str(r.get('name') or r.get('agent_id') or '')+'**\n\n'+str(r.get('output') or '') for r in turn.results)
    if isinstance(turn, ThoughtMessageTurn):
        parts = []
        for segment in turn.export_segments():
            if segment.get('type') == 'reasoning':
                parts.append(str(segment.get('text') or ''))
            elif segment.get('type') == 'tool':
                parts.append('**{}**\n\n```json\n{}\n```\n\n{}'.format(
                    segment.get('name') or tr('ui.caption.a8ba5e181272'),
                    json.dumps(segment.get('args') or {}, ensure_ascii=False, indent=2),
                    segment.get('output') or ''))
        return tr('ui.caption.3b59907c92ae'), '\n\n'.join(parts) or turn.total_thought_text
    return tr('ui.caption.e69724d99d32'), turn.raw_content


def copy_from_menu(turn, markdown):
    _, text = message_markdown(turn)
    if not markdown:
        from ui.views.plain_message import plain_message
        text = plain_message(text)
    QApplication.clipboard().setText(text)


class SelectionGutter(QWidget):
    def __init__(self, controller):
        super().__init__(controller.chat)
        self.controller = controller
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        set_text(self, 'ui.chat_selection.drag_along_the_left_side_of_c0ab', setter='setToolTip')
        self.anchor = None
        self.base = set()
        self.selecting = True
        self.press_position = None
        self.copy_candidate = None
        self.dragging = False
        self.scroll_timer = QTimer(self)
        self.scroll_timer.setInterval(30)
        self.scroll_timer.timeout.connect(self.auto_scroll)

    def rectangles(self):
        rows, tops, bottoms = self.controller.row_geometry()
        offset = self.controller.chat.turns_container.mapTo(
            self.controller.chat, QPoint(0, 0)).y() - self.y()
        first = bisect_left(bottoms, -offset)
        last = bisect_right(tops, self.height() - offset)
        for index in range(first, last):
            yield rows[index], QRect(0, tops[index] + offset, self.width(),
                                     bottoms[index] - tops[index] + 1)

    def copy_rect(self):
        return QRect(max(0, self.width() - 64), (self.height() - 24) // 2,
                     min(24, self.width()), 24)

    def at(self, y, *, nearest=False):
        rows, tops, bottoms = self.controller.row_geometry()
        if not rows:
            return None
        offset = self.controller.chat.turns_container.mapTo(
            self.controller.chat, QPoint(0, 0)).y() - self.y()
        position = y - offset
        index = min(bisect_left(bottoms, position), len(rows) - 1)
        if not tops[index] <= position <= bottoms[index]:
            if not (self.dragging or nearest):
                return None
            if index > 0 and abs(position - bottoms[index-1]) < abs(tops[index] - position):
                index -= 1
        return rows[index]

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        icon = self.copy_rect()
        painter.setPen(QPen(QColor('#a1a1aa' if self.controller.selected else '#52525b'), 1.4))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(icon.adjusted(8, 3, -4, -8))
        painter.drawRect(icon.adjusted(4, 7, -8, -4))
        if not self.controller.active:
            return
        for turn, rect in self.rectangles():
            if rect.bottom() < 0 or rect.top() > self.height():
                continue
            y = rect.top() + min(24, max(10, rect.height() // 2))
            chosen = turn in self.controller.selected
            painter.setPen(QPen(theme_manager.color('accent.primary' if chosen else 'text.disabled'), 1.4))
            painter.setBrush(QColor('#6366f1') if chosen else Qt.NoBrush)
            x = max(7, self.width() - 18)
            painter.drawEllipse(QPoint(x, y), 7, 7)
            if chosen:
                painter.setPen(QColor('white'))
                painter.drawText(QRect(x - 7, y - 8, 14, 16), Qt.AlignCenter, '✓')

    def start_range(self):
        if self.anchor is None and self.press_position is not None:
            # A click in a gap does nothing, but a drag may start anywhere in
            # the gutter. Resolve its anchor only once dragging is confirmed.
            self.anchor = self.at(self.press_position.y(), nearest=True)
        if self.anchor is None:
            return
        self.base = set(self.controller.selected)
        self.selecting = self.anchor not in self.base
        self.controller.active = True
        self.dragging = True
        self.update_range(self.anchor)
        self.scroll_timer.start()

    def update_range(self, last):
        rows = self.controller.visible_turns()
        indexes = self.controller.turn_indexes()
        if self.anchor not in indexes or last not in indexes:
            return
        a, b = sorted((indexes[self.anchor], indexes[last]))
        from ui.views.chat_model_blocks import expand
        interval = expand(rows[a:b + 1])
        selected = self.base | interval if self.selecting else self.base - interval
        self.controller.set_selected(selected)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton or self.controller.busy:
            return super().mousePressEvent(event)
        self.anchor = self.at(event.position().y())
        self.press_position = event.position().toPoint()
        self.copy_candidate = self.copy_rect().contains(self.press_position)
        event.accept()

    def mouseMoveEvent(self, event):
        if self.press_position is None or not event.buttons() & Qt.LeftButton:
            return
        if not self.dragging and (event.position().toPoint() - self.press_position).manhattanLength() >= QApplication.startDragDistance():
            self.copy_candidate = None
            self.start_range()
        if self.dragging:
            self.update_range(self.at(event.position().y()))
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            if self.dragging:
                self.update_range(self.at(event.position().y()))
            elif self.copy_candidate and self.copy_rect().contains(event.position().toPoint()):
                if self.controller.selected:
                    self.controller.copy(False)
            elif self.controller.active and self.anchor is not None:
                from ui.views.chat_model_blocks import expand
                self.controller.set_selected(self.controller.selected ^ expand([self.anchor]))
            self.cancel_drag()
            if not self.controller.selected:
                self.controller.cancel()
            event.accept()

    def cancel_drag(self):
        self.scroll_timer.stop()
        self.anchor = None
        self.press_position = None
        self.copy_candidate = None
        self.dragging = False

    def auto_scroll(self):
        if not QApplication.mouseButtons() & Qt.LeftButton:
            self.cancel_drag()
            return
        point = self.mapFromGlobal(QCursor.pos())
        delta = -14 if point.y() < 24 else 14 if point.y() > self.height() - 24 else 0
        if delta:
            bar = self.controller.chat.scroll_area.verticalScrollBar()
            bar.setValue(bar.value() + delta)
            self.update_range(self.at(point.y()))


class MessageSelection(QObject):
    def __init__(self, chat, composer_layout):
        super().__init__(chat)
        self.chat = chat
        self.selected = set()
        self.original_styles = {}
        self.active = False
        theme_manager.theme_changed.connect(self._refresh_theme)
        self.busy = False
        self.revision = 0
        self._previous_auto_scroll = None
        self._layout_sync_pending = False
        self._turns_key = None
        self._turns_cache = []
        self._turn_members = set()
        self._turn_indexes = {}
        self._rows_key = None
        self._rows_cache = ([], [], [])
        self._geometry_revision = 0
        self._actions_active = None
        self._action_turns = set()
        self._actions_key = None
        self._selection_ui_active = False
        self.gutter = SelectionGutter(self)
        chat.turns_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar_holder = QWidget(chat.center_col)
        self.toolbar_holder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.toolbar_layout = QHBoxLayout(self.toolbar_holder)
        self.toolbar_layout.setContentsMargins(0, 0, 0, 0)
        self.toolbar = QFrame(self.toolbar_holder)
        self.toolbar.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.toolbar_layout.addWidget(self.toolbar)
        self.toolbar.setObjectName('MessageSelectionToolbar')
        apply_style(self.toolbar, '#MessageSelectionToolbar {background:@background.panel@; border:1px solid @border.subtle@; border-radius:8px;}'
            '#MessageSelectionToolbar QToolButton {color:@text.primary@; background:transparent; border:none; padding:7px 5px;}'
            '#MessageSelectionToolbar QToolButton:hover {background:@control.hover@; border-radius:4px;}')
        rows = QHBoxLayout(self.toolbar)
        rows.setContentsMargins(10, 12, 10, 12)
        rows.setSpacing(8)
        self.count = QLabel('0', self.toolbar)
        apply_style(self.count, 'color:@text.primary@; font-size:12px; background:transparent; border:none;')
        rows.addWidget(self.count)
        rows.addStretch()
        self.action_buttons = []
        for label, tip, action in [
            ('⌫', tr('ui.caption.d6d2df36a346'), self.delete),
            ('T', tr('ui.caption.a47a1d8180ba'), lambda: self.copy(False)),
            ('MD', tr('ui.caption.2db79192be44'), lambda: self.copy(True)),
            ('↗', tr('ui.caption.a2d9295bbe67'), self.choose_branch),
            ('▣', tr('ui.caption.011a08061892'), self.choose_session),
            ('×', tr('ui.caption.5799e216af56'), self.cancel),
        ]:
            button = QToolButton(self.toolbar)
            from ui.icons import set_icon
            set_icon(button, {'⌫':'delete', 'T':'document', 'MD':'markdown', '↗':'branch', '▣':'forward', '×':'close'}[label], size=18); bind_caption(button, tip, setter='setToolTip'); button.setCursor(Qt.PointingHandCursor)
            bind_caption(button, tip, setter='setAccessibleName')
            from ui.views.chat_references import square_button
            square_button(button, 32)
            button.clicked.connect(action)
            rows.addWidget(button)
            self.action_buttons.append(button)
        # Оба режима занимают одно место и одну геометрию в компоновке.
        from PySide6.QtWidgets import QStackedWidget
        self.composer_stack = QStackedWidget(chat.center_col)
        self.composer_stack.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        index = composer_layout.indexOf(chat.input_card)
        composer_layout.removeWidget(chat.input_card)
        self.composer_stack.addWidget(chat.input_card)
        self.composer_stack.addWidget(self.toolbar_holder)
        composer_layout.insertWidget(index, self.composer_stack)
        chat.input_card.installEventFilter(self)
        self.toolbar_holder.hide()
        self.viewport = chat.scroll_area.viewport()
        self.viewport.installEventFilter(self)
        chat.turns_container.installEventFilter(self)
        chat.scroll_area.verticalScrollBar().valueChanged.connect(self.gutter.update)
        QApplication.instance().installEventFilter(self)
        self.position_gutter()

    def turns(self):
        layout = self.chat.turns_layout
        key = (getattr(layout, 'structure_revision', None), layout.count())
        if key == self._turns_key:
            return self._turns_cache
        from ui.views.chat_view import UserMessageTurn, AssistantMessageTurn, ThoughtMessageTurn
        rows = [widget for i in range(layout.count())
                if isinstance((widget := layout.itemAt(i).widget()),
                              (UserMessageTurn, AssistantMessageTurn, ThoughtMessageTurn))]
        members = set(rows)
        for turn in members - self._turn_members:
            turn.installEventFilter(self)
        for turn in self._turn_members - members:
            if isValid(turn):
                turn.removeEventFilter(self)
        self._turns_cache = rows
        self._turn_members = members
        self._turn_indexes = {turn: index for index, turn in enumerate(rows)}
        self._turns_key = key
        self._rows_key = None
        from ui.views.chat_model_blocks import refresh
        self._visible_turns = refresh(rows)
        self._visible_indexes = {turn: index for index, turn in enumerate(self._visible_turns)}
        if self.selected:
            self.set_selected(self.selected)
        return rows

    def visible_turns(self):
        self.turns()
        return self._visible_turns

    def turn_indexes(self):
        self.turns()
        return self._visible_indexes

    def invalidate_geometry(self):
        self._geometry_revision += 1
        self._rows_key = None

    def row_geometry(self):
        rows = self.visible_turns()
        key = (self._turns_key, self._geometry_revision,
               getattr(self.chat.turns_layout, 'geometry_revision', None))
        if key != self._rows_key:
            from ui.views.chat_model_blocks import members
            tops = [members(turn)[0].mapTo(self.chat.turns_container, QPoint(0, 0)).y() for turn in rows]
            from ui.views.chat_highlight import content_rect
            bottoms = [members(turn)[-1].mapTo(self.chat.turns_container, QPoint(0, 0)).y() + content_rect(members(turn)[-1]).height() - 1 for turn in rows]
            self._rows_cache = (rows, tops, bottoms)
            self._rows_key = key
        return self._rows_cache

    def position_gutter(self):
        if not isValid(self.chat) or not isValid(self.gutter):
            return
        viewport = self.chat.scroll_area.viewport()
        origin = viewport.mapTo(self.chat, QPoint(0, 0))
        # The selection controls occupy only the free space to the left;
        # they never reserve width in the message column or cover its text.
        self.gutter.setGeometry(0, origin.y(), max(0, origin.x()), viewport.height())
        self.gutter.setVisible(self.chat.scroll_area.isVisible())
        self.gutter.raise_()
        self.gutter.update()
        if hasattr(self.chat, 'chat_references'):
            self.chat.chat_references.schedule_position()

    def begin(self, turn):
        if self.busy:
            return
        self.active = True
        self.set_selected(self.selected | {turn})

    def update_message_actions(self):
        self.turns()
        if self._actions_active == self.active and self._actions_key == self._turns_key:
            return
        turns = (self._turn_members - self._action_turns
                 if self._actions_active == self.active else self._turn_members)
        for turn in turns:
            bar = getattr(turn, 'actions_bar', None)
            if bar is not None:
                bar.refresh_visibility()
        self._actions_active = self.active
        self._actions_key = self._turns_key
        self._action_turns = self._turn_members.copy()

    def schedule_layout_sync(self):
        if getattr(self.chat, '_session_loading', False):
            return
        if not self._layout_sync_pending:
            self._layout_sync_pending = True
            QTimer.singleShot(0, self.sync_message_layout)

    def sync_message_layout(self):
        self._layout_sync_pending = False
        if getattr(self.chat, '_session_loading', False):
            return
        if not isValid(self.chat) or not isValid(self.toolbar_holder):
            return
        self.update_message_actions()

    def set_selected(self, chosen):
        self.turns()
        from ui.views.chat_model_blocks import expand, owner
        chosen = expand(chosen)
        chosen = chosen & self._turn_members
        changed = self.selected ^ chosen
        mode_changed = self.active != self._selection_ui_active
        if not changed and not mode_changed:
            return
        self.selected = chosen
        for turn in changed:
            if not isValid(turn):
                self.original_styles.pop(turn, None)
                continue
            if turn not in self.original_styles:
                flash = getattr(owner(turn), 'navigation_flash', None)
                self.original_styles[turn] = flash.base_styles.get(turn, turn.styleSheet()) if flash is not None else turn.styleSheet()
            turn.setProperty('messageSelected', turn in chosen)
            bar = getattr(turn, 'actions_bar', None)
            if bar is not None:
                bar.refresh_visibility()
            self.apply_turn_style(turn)
        bind_caption(self.count, str(len({owner(turn) for turn in chosen})) + tr('ui.caption.2004770364c9'))
        self.update_message_actions()
        if mode_changed and self.active and self._previous_auto_scroll is None:
            self.composer_stack.setFixedHeight(self.chat.input_card.height())
            self._previous_auto_scroll = self.chat._auto_scroll_to_bottom
            self.chat._auto_scroll_to_bottom = False
        if mode_changed:
            if not self.active:
                self.composer_stack.setMinimumHeight(0)
                self.composer_stack.setMaximumHeight(16777215)
            self.composer_stack.setCurrentWidget(self.toolbar_holder if self.active else self.chat.input_card)
            self.chat._schedule_overlay_update()
            if self.active:
                self.chat.attachments_panel.hide()
                self.chat.tags_bar.hide()
            self._selection_ui_active = self.active
        self.gutter.update()

    def _refresh_theme(self, *_):
        for turn in list(self.selected):
            if isValid(turn):
                self.apply_turn_style(turn)
        self.gutter.update()

    def apply_turn_style(self, turn):
        from ui.views.chat_model_blocks import owner
        from ui.views.chat_highlight import set_highlight
        flash = getattr(owner(turn), 'navigation_flash', None)
        style = self.original_styles.get(turn, flash.base_styles.get(turn, turn.styleSheet()) if flash is not None else turn.styleSheet())
        color = theme_manager.value('control.selected') if turn in self.selected else None
        if turn in self.selected:
            if hasattr(turn, 'bubble'):
                style += '\n#UserMessageBubble {background-color:' + theme_manager.value('control.selected') + '; border:1px solid ' + theme_manager.value('accent.primary') + ';}'
        if flash is not None:
            if flash.strength > 0:
                color = flash.color(turn in self.selected).name()
            style += flash.rule(turn in self.selected, turn)
        if turn.styleSheet() != style:
            turn.setStyleSheet(style)
        set_highlight(turn, color)
        from ui.views.chat_view import MarkdownMessageWidget
        for renderer in turn.findChildren(MarkdownMessageWidget):
            selected = turn in self.selected
            if renderer.property('messageSelected') != selected:
                renderer.setProperty('messageSelected', selected)
                renderer._theme_changed()

    def cancel(self, force=False):
        if self.busy and not force:
            return
        self.gutter.cancel_drag()
        self.revision += 1
        self.active = False
        self.set_selected(set())
        self.original_styles.clear()
        if self._previous_auto_scroll is not None:
            self.chat._auto_scroll_to_bottom = self._previous_auto_scroll
            self._previous_auto_scroll = None
        self.chat._refresh_pending_attachments_panel()
        self.chat._position_tags_bar()

    def eventFilter(self, watched, event):
        kind = event.type()
        if watched is self.chat.input_card and kind in (QEvent.Resize, QEvent.LayoutRequest) and not self.active:
            self.composer_stack.setFixedHeight(self.chat.input_card.sizeHint().height())
        if kind not in _FILTER_EVENTS:
            return False
        if not isValid(self.chat) or not isValid(self.chat.turns_layout) or not isValid(self.viewport):
            return False
        if (getattr(self.chat, '_session_loading', False)
                and kind in (QEvent.ShortcutOverride, QEvent.KeyPress, QEvent.MouseButtonPress)
                and isinstance(watched, QWidget)
                and self.chat.turns_container.isAncestorOf(watched)):
            event.accept()
            return True
        # This filter is application-wide: watched may be a QInputDevice or
        # another non-widget QObject whose Python wrapper is not hashable.
        # Only message-widget geometry/deletion events need set membership.
        is_turn = (kind in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide, QEvent.DeferredDelete)
                   and isinstance(watched, QWidget) and watched in self._turn_members)
        if is_turn and kind in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide):
            self.invalidate_geometry()
            self.gutter.update()
        if is_turn and kind == QEvent.DeferredDelete:
            self._turns_key = None
        if (self.active and not self.busy and self.chat.isVisible()
                and event.type() in (QEvent.ShortcutOverride, QEvent.KeyPress)
                and event.key() == Qt.Key_Escape):
            window = self.chat.window()
            def belongs_to_window(widget):
                # isAncestorOf stops at popup window boundaries; parentWidget
                # still retains the menu's owning application window.
                while widget is not None:
                    if widget is window:
                        return True
                    widget = widget.parentWidget()
                return False
            popup = QApplication.activePopupWidget()
            in_window = (belongs_to_window(watched) if isinstance(watched, QWidget)
                         else watched is window.windowHandle() or (
                             popup is not None and watched is popup.windowHandle()
                             and belongs_to_window(popup)))
            if in_window:
                # Hiding the composer can move focus out of ChatView. Reserve
                # Escape before other shortcuts, then handle it for this window.
                event.accept()
                if event.type() == QEvent.KeyPress:
                    if popup is not None and belongs_to_window(popup):
                        popup.close()
                    self.cancel()
                return True
        if watched in (self.viewport, self.chat, self.chat.center_col, self.chat.scroll_area, self.toolbar_holder) and event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide):
            self.position_gutter()
            self.schedule_layout_sync()
        elif watched is self.chat.turns_container and event.type() in (QEvent.LayoutRequest, QEvent.Resize, QEvent.ChildAdded, QEvent.ChildRemoved):
            self.invalidate_geometry()
            if kind in (QEvent.ChildAdded, QEvent.ChildRemoved):
                self._turns_key = None
            self.gutter.update()
            self.schedule_layout_sync()
            if not getattr(self.chat, '_session_loading', False):
                self.turns()
                if self.selected - self._turn_members:
                    QTimer.singleShot(0, self.prune_selection)
        if self.active and not self.busy and event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            if (isinstance(watched, QWidget) and watched is not self.gutter
                    and self.chat.isAncestorOf(watched)):
                self.turns()
                turns = self._turn_members
                current = watched
                while current is not None:
                    from ui.views.chat_presentation import MessageActions
                    if isinstance(current, MessageActions):
                        event.accept()
                        return True
                    if current in turns:
                        from ui.views.chat_model_blocks import expand
                        chosen = self.selected ^ expand([current])
                        self.set_selected(chosen)
                        if not chosen:
                            self.cancel()
                        return True
                    current = current.parentWidget()
        return False

    def prune_selection(self):
        if not isValid(self.chat) or not isValid(self.chat.turns_layout):
            return
        self.set_selected(self.selected & set(self.turns()))
        if self.active and not self.selected and not self.gutter.dragging and not self.busy:
            self.cancel()

    def copy(self, markdown, turns=None):
        selected = set(turns) if turns is not None else self.selected
        parts = []
        for turn in self.turns():
            if turn not in selected:
                continue
            author, text = message_markdown(turn)
            if markdown:
                parts.append('### ' + author + '\n\n' + text)
            else:
                from ui.views.plain_message import plain_message
                parts.append(author + ':\n' + plain_message(text))
        if not parts:
            return
        QApplication.clipboard().setText('\n\n'.join(parts))
        if self.active:
            self.cancel()

    def can_change(self):
        if self.busy or not self.selected:
            return False
        if self.chat.current_worker and self.chat.current_worker.isRunning():
            self.chat._show_error_panel(tr('ui.caption.14d7c0283ef3'))
            return False
        return True

    def delete(self):
        if not self.can_change():
            return
        chosen = set(self.selected)
        source = (self.chat.current_session_id, self.chat.current_branch, self.revision)

        def remove_confirmed():
            if not isValid(self.chat) or source != (self.chat.current_session_id, self.chat.current_branch, self.revision):
                return
            if self.selected != chosen or not self.can_change():
                return
            self.remove_turns(chosen)
            self.cancel()
            self.chat._sync_session_to_disk()

        from ui.views.chat_model_blocks import owner
        count = len({owner(turn) for turn in chosen})
        if count > 1:
            inline.confirm(self.chat, tr('ui.chat_selection.delete_messages_b50b'),
                           tr('ui.chat_selection.delete_selected_messages_v0_b380', v0=count), remove_confirmed)
        else:
            remove_confirmed()

    def remove_turns(self, turns):
        from ui.views.chat_model_blocks import expand
        for turn in expand(turns):
            # Remove styles while the wrappers are still alive.
            if turn in self.original_styles:
                turn.setStyleSheet(self.original_styles.pop(turn))
            self.selected.discard(turn)
            if turn in self.chat.user_turn_widgets:
                self.chat.user_turn_widgets.remove(turn)
            self.chat.turns_layout.removeWidget(turn)
            turn.hide()
            turn.deleteLater()
        self.chat.timeline_rail.update_turns(self.chat.user_turn_widgets)

    def choose_branch(self):
        if self.can_change():
            inline.text_input(self.chat, tr('ui.chat_selection.new_branch_selected_messages_only_bcc1'), '',
                lambda name: self.move(self.chat.current_session_id, name, new_branch=True))

    def choose_session(self):
        if not self.can_change():
            return
        source = self.chat.current_session_id
        def show(items):
            if not self.active or self.chat.current_session_id != source:
                return
            options = [(tr('ui.caption.1da83207fbe8'), None)] + [(identity, identity) for identity in items if identity != source]
            inline.choose_item(self.chat, tr('ui.caption.8e7431ce1f39'), options, self.move)
        self.chat.session_write_runner.submit(self.chat.session_manager.list_sessions,
            on_success=show, on_error=lambda error: self.chat._show_error_panel(str(error)))

    def forward_turn(self, turn):
        chat = self.chat
        if self.busy or turn not in self.turns() or not chat.current_session_id:
            return
        if chat.current_worker and chat.current_worker.isRunning():
            chat._show_error_panel(tr('ui.caption.18984f8dfa6a'))
            return
        source, branch = chat.current_session_id, chat.current_branch
        generation = chat._session_load_generation
        def choose(items):
            if ((chat.current_session_id, chat.current_branch, chat._session_load_generation) != (source, branch, generation)
                    or turn not in self.turns()):
                return
            options = [(tr('ui.caption.2f8dd86c5b89'), None)] + [(identity, identity) for identity in items if identity != source]
            inline.choose_item(chat, tr('ui.caption.54363bd6bd68'), options,
                               lambda target: self._forward_to(turn, target, source, branch, generation),
                               anchor=turn.actions_bar.buttons['forward'])
        chat.session_write_runner.submit(chat.session_manager.list_sessions, on_success=choose,
            on_error=lambda error: chat._show_error_panel(tr('ui.caption.1f4f1a7df48b') + str(error)))

    def _forward_to(self, turn, destination, source, branch, generation):
        chat = self.chat
        if (self.busy or turn not in self.turns()
                or (chat.current_session_id, chat.current_branch, chat._session_load_generation) != (source, branch, generation)):
            return
        if chat.current_worker and chat.current_worker.isRunning():
            chat._show_error_panel(tr('ui.caption.18984f8dfa6a'))
            return
        snapshot, _ = chat._snapshot_turn_messages(include_prefix=True)
        from ui.views.chat_model_blocks import members
        selected, _ = chat._snapshot_turn_messages(members(turn))
        if not selected:
            return
        chat._sync_session_to_disk()
        self.busy = True
        chat.turns_container.setEnabled(False)
        project_id = app_state.project_id or None
        def transfer():
            target, created = destination, None
            try:
                if target is None:
                    _, metadata, _ = chat.session_manager.load_session(source, branch=branch)
                    keys = {'temp_memory_mode', 'limit_ranking', 'include_thoughts_in_context', 'workspace_root'}
                    created = chat.session_manager.create_session({key: value for key, value in metadata.items() if key in keys},
                        folder_id=project_id)
                    target = created['session_id']
                return chat.session_manager.forward_messages(source, branch=branch, snapshot=snapshot,
                    selected=selected, destination_session=target)
            except Exception:
                if created is not None:
                    try:
                        history, _, _ = chat.session_manager.load_session(created['session_id'], branch='main')
                        if not history:
                            chat.session_manager.delete_session(created['session_id'])
                    except Exception:
                        pass
                raise
        def finish(result):
            if not isValid(chat):
                return
            self.busy = False
            chat.turns_container.setEnabled(True)
            cached = chat._session_cache.get((result.get('session_id'), result.get('branch', 'main')))
            if cached:
                cached['stale'] = True
            event_bus.sessions_updated.emit()
            if (chat.current_session_id, chat.current_branch, chat._session_load_generation) == (source, branch, generation) and chat.isVisible():
                inline.notice(chat, tr('ui.chat_selection.message_forwarded_2cd1'), tr('ui.caption.44f7f58155af') + result['session_id'] + '».')
        def fail(error):
            if isValid(chat):
                self.busy = False
                chat.turns_container.setEnabled(True)
                chat._show_error_panel(tr('ui.caption.6a855749a97e') + str(error))
        chat.session_write_runner.submit(transfer, on_success=finish, on_error=fail)

    def move(self, destination=None, branch='main', new_branch=False):
        if not self.can_change():
            return
        chat = self.chat
        source, source_branch = chat.current_session_id, chat.current_branch
        revision = self.revision
        chosen = set(self.selected)
        rows = self.turns()
        snapshot, _ = chat._snapshot_turn_messages(rows, include_prefix=True)
        selected, _ = chat._snapshot_turn_messages([turn for turn in rows if turn in chosen])
        remaining, history = chat._snapshot_turn_messages([turn for turn in rows if turn not in chosen], include_prefix=True)
        # Queue a current snapshot before the checked move on the same serial
        # runner. The core rejects a stale source or a running destination.
        chat._sync_session_to_disk()
        self.busy = True
        self.toolbar.setEnabled(False)
        chat.turns_container.setEnabled(False)
        project_id = app_state.project_id or None
        def transfer():
            target = destination
            created = None
            try:
                if target is None:
                    _, metadata, _ = chat.session_manager.load_session(source, branch=source_branch)
                    keys = {'temp_memory_mode', 'limit_ranking', 'include_thoughts_in_context', 'active_tags', 'workspace_root'}
                    new_metadata = {key: value for key, value in metadata.items() if key in keys}
                    new_metadata['include_thoughts_in_context'] = True
                    created = chat.session_manager.create_session(new_metadata,
                        folder_id=project_id)
                    target = created['session_id']
                return chat.session_manager.move_selection(source, branch=source_branch, snapshot=snapshot,
                    selected=selected, remaining=remaining, destination_session=target,
                    destination_branch=branch, new_branch=new_branch)
            except Exception:
                if created is not None:
                    try:
                        messages, _, _ = chat.session_manager.load_session(created['session_id'], branch='main')
                        # A lost HTTP response can follow a committed move.
                        # Never delete a destination that already has messages.
                        if not messages:
                            chat.session_manager.delete_session(created['session_id'])
                    except Exception:
                        pass
                raise
        def finish(result):
            if not isValid(chat):
                return
            self.busy = False
            self.toolbar.setEnabled(True)
            chat.turns_container.setEnabled(True)
            if (chat.current_session_id, chat.current_branch) == (source, source_branch) and self.revision == revision:
                self.remove_turns(chosen)
                visible, chat.messages_history = chat._snapshot_turn_messages()
                chat._history_anchor = visible[0].additional_kwargs.get('chat_id') if visible else None
                self.cancel()
            cached = chat._session_cache.get((result.get('session_id'), result.get('branch', 'main')))
            if cached:
                cached['stale'] = True
            event_bus.sessions_updated.emit()
        def fail(error):
            if not isValid(chat):
                return
            self.busy = False
            self.toolbar.setEnabled(True)
            chat.turns_container.setEnabled(True)
            chat._show_error_panel(tr('ui.caption.af3c8b07b6a6') + str(error))
        chat.session_write_runner.submit(transfer, on_success=finish, on_error=fail)
