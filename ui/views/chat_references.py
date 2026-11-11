"""Pinned-message navigation and exact, request-scoped replies."""
from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text
from ui.styles.binding import apply_style
from ui.icons import set_icon, symbol_button

from ui.qt_deferred import defer
from copy import deepcopy

from shiboken6 import isValid
from PySide6.QtCore import QObject, QEvent, QPoint, QRect, Qt, QTimer, QVariantAnimation, QEasingCurve, QSize
from PySide6.QtGui import QColor, QPalette, QTextDocument, QPainter, QPen, QPixmap, QIcon
from PySide6.QtWidgets import (QApplication, QFrame, QHBoxLayout, QLabel, QMenu,
    QSizePolicy, QTextEdit, QToolButton, QVBoxLayout, QWidget, QWidgetAction)

from ui.views.quote_selection import QuoteSelectionSession

from core.chat_references import CHAT_ID, PINNED, REPLY, REQUEST_MEMORY, new_chat_id, valid_chat_id


_FILTER_EVENTS = frozenset((QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide,
    QEvent.LayoutRequest, QEvent.ChildAdded, QEvent.ChildRemoved,
    QEvent.ContextMenu, QEvent.MouseButtonRelease))


def turn_metadata(turn):
    if not hasattr(turn, 'chat_meta'):
        turn.chat_meta = {CHAT_ID: new_chat_id(), PINNED: False}
    return deepcopy(turn.chat_meta)


def restore_turn(turn, data):
    turn.chat_meta = {CHAT_ID: data.get(CHAT_ID) if valid_chat_id(data.get(CHAT_ID)) else new_chat_id(),
                      PINNED: bool(data.get(PINNED, False))}
    for key in ('task_status_check', 'task_status_task_ids', 'task_status_display', 'task_status_refresh'):
        if key in data:
            turn.chat_meta[key] = deepcopy(data[key])
    if data.get('full_web_pages'):
        turn.chat_meta['full_web_pages'] = True
    for key in ('requested_resources', 'module_context'):
        if isinstance(data.get(key), dict):
            turn.chat_meta[key] = deepcopy(data[key])
    from ui.views.chat_request_badges import BADGES
    if isinstance(data.get(BADGES), list):
        turn.chat_meta[BADGES] = deepcopy(data[BADGES])
    if isinstance(data.get(REPLY), dict):
        turn.chat_meta[REPLY] = deepcopy(data[REPLY])
    if isinstance(data.get(REQUEST_MEMORY), dict):
        turn.chat_meta[REQUEST_MEMORY] = deepcopy(data[REQUEST_MEMORY])
    if isinstance(data.get('request_clock'), dict):
        turn.chat_meta['request_clock'] = deepcopy(data['request_clock'])
    controller = controller_for(turn)
    if controller is not None:
        controller.invalidate_turn_index()
    refresh_badges = getattr(turn, '_refresh_tag_badges', None)
    if refresh_badges is not None:
        refresh_badges()


def controller_for(turn):
    parent = turn.parentWidget()
    while parent is not None:
        controller = getattr(parent, 'chat_references', None)
        if controller is not None:
            return controller
        parent = parent.parentWidget()


def install_reply_button(turn, header):
    button = QToolButton(turn)
    button.setObjectName('MessageActionBtn')
    set_icon(button, 'reply', square=True)
    set_text(button, 'ui.chat_references.reply_to_a_message_or_selected_86ad', setter='setToolTip')
    button.setFocusPolicy(Qt.NoFocus)
    square_button(button)
    policy = button.sizePolicy(); policy.setRetainSizeWhenHidden(True); button.setSizePolicy(policy)
    button.clicked.connect(lambda: controller_for(turn).prepare_reply(turn)
                           if controller_for(turn) is not None else None)
    button.hide()
    turn.reply_btn = button
    header.addWidget(button)


def add_reference_actions(turn, menu):
    controller = controller_for(turn)
    if controller is None:
        return
    controller.extend_menu(turn, menu)


def square_button(button, size=28):
    button.setFixedSize(size, size)
    button.setStyleSheet(f'QToolButton {{padding:0; min-width:{size}px; max-width:{size}px;'
                        f' min-height:{size}px; max-height:{size}px;}}')


class MessageFlash(QObject):
    """Animate the card background while leaving text and selection intact."""
    def __init__(self, turn, selection):
        super().__init__(turn)
        self.turn = turn
        self.selection = selection
        self.base_style = selection.original_styles.get(turn, turn.styleSheet())
        from ui.views.chat_model_blocks import members
        self.base_styles = {item: selection.original_styles.get(item, item.styleSheet()) for item in members(turn)}
        self.base_color = theme_manager.color('background.window')
        self.strength = 0.0
        self.animation = QVariantAnimation(self)
        self.animation.setDuration(1500)
        self.animation.setStartValue(0.0)
        self.animation.setKeyValueAt(0.08, 0.36)
        self.animation.setKeyValueAt(0.28, 0.36)
        self.animation.setEndValue(0.0)
        self.animation.setEasingCurve(QEasingCurve.Linear)
        self.animation.valueChanged.connect(self.update)
        self.animation.finished.connect(self.finish)

    def color(self, selected):
        base = theme_manager.color('control.selected' if selected else 'background.window')
        tint = theme_manager.color('accent.primary')
        return QColor(*(round(a + (b-a)*self.strength) for a, b in zip(
            (base.red(), base.green(), base.blue()), (tint.red(), tint.green(), tint.blue()))))

    def rule(self, selected, turn=None):
        turn = turn or self.turn
        if self.strength <= 0:
            return ''
        # The row color is painted only behind content by MessageHighlight.
        rule = ''
        tint = theme_manager.color('accent.primary')
        if hasattr(turn, 'bubble') and not selected:
            base = theme_manager.color('background.surface')
            bubble = QColor(*(round(a + (b-a)*self.strength) for a, b in zip(
                (base.red(), base.green(), base.blue()), (tint.red(), tint.green(), tint.blue()))))
            rule += '\n#UserMessageBubble {background-color:' + bubble.name() + ';}'
        return rule

    def update(self, value):
        if isValid(self.turn):
            self.strength = float(value)
            from ui.views.chat_model_blocks import members
            for item in members(self.turn):
                self.selection.apply_turn_style(item)

    def finish(self):
        if isValid(self.turn):
            self.strength = 0.0
            from ui.views.chat_model_blocks import members
            for item in members(self.turn):
                self.selection.apply_turn_style(item)
            self.turn.navigation_flash = None
        self.deleteLater()


class PreviewButton(QToolButton):
    """Plain-text preview with at most two lines of message content."""
    def __init__(self, parent=None, *, tooltip=True):
        super().__init__(parent)
        self._preview_tooltip = tooltip
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        apply_style(self, 'QToolButton {background:transparent; border:none; text-align:left;}QToolButton:hover {background:@control.hover@;} QLabel {background:transparent; border:none;}')
        layout = QVBoxLayout(self); layout.setContentsMargins(8, 4, 8, 4); layout.setSpacing(1)
        self.title = QLabel(self); self.title.setStyleSheet('color:#a5b4fc; font-size:12px; font-weight:600; background:transparent;')
        self.body = QLabel(self); self.body.setWordWrap(True)
        self.body.setStyleSheet('font-size:12px; background:transparent;')
        self.body.setAlignment(Qt.AlignLeft | Qt.AlignTop)
        for label in (self.title, self.body):
            label.setTextFormat(Qt.PlainText)
            label.setAttribute(Qt.WA_TransparentForMouseEvents)
            label.setMinimumWidth(0)
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.title.setFixedHeight(self.title.fontMetrics().lineSpacing())
        self.body.setFixedHeight(self.body.fontMetrics().lineSpacing() * 2)
        layout.addWidget(self.title); layout.addWidget(self.body)
        self.setFixedHeight(self.title.fontMetrics().lineSpacing() + self.body.height() + 10)

    def preview(self, title, text):
        text = str(text or '')[:500]
        if self.title.text() == title and self.body.text() == text:
            return False
        if self.title.text() != title: bind_caption(self.title, title)
        if self.body.text() != text: bind_caption(self.body, text)
        if self._preview_tooltip:
            bind_caption(self, title + '\n' + text, setter='setToolTip')
        return True


class ReplyPreviewButton(PreviewButton):
    """Message reply preview with the same left accent as Markdown quotes."""
    def __init__(self, parent=None):
        super().__init__(parent, tooltip=False)
        self.layout().setContentsMargins(12, 0, 8, 0)

    def _fit_content(self):
        margins = self.layout().contentsMargins()
        width = max(1, self.width() - margins.left() - margins.right())
        metrics = self.body.fontMetrics()
        height = metrics.boundingRect(QRect(0, 0, width, 10000), Qt.TextWordWrap, self.body.text()).height()
        height = min(metrics.lineSpacing() * 2, max(metrics.lineSpacing(), height))
        self.body.setFixedHeight(height)
        self.setFixedHeight(self.title.height() + self.layout().spacing() + height)

    def preview(self, title, text):
        if super().preview(title, text):
            self._fit_content()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_content()

    def paintEvent(self, event):
        super().paintEvent(event)
        metrics = self.body.fontMetrics()
        text_height = metrics.boundingRect(self.body.rect(), Qt.TextWordWrap, self.body.text()).height()
        text_height = min(self.body.height(), max(metrics.lineSpacing(), text_height))
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#38bdf8'), 3, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(2, self.title.y() + 1, 2, self.body.y() + text_height - 2)
        painter.end()


class ChatReferences(QObject):
    def __init__(self, chat, composer_layout):
        super().__init__(chat)
        self.chat = chat
        self.pending = None
        self.quote_selection = QuoteSelectionSession(self)
        self.next_pin = None
        self.refresh_pending = False
        self.last_selection_widget = None
        self.pin_updates = {}
        self.pin_inflight = set()
        self._turn_index = {}
        self._turn_index_key = None
        self._identity_revision = 0
        self._plain_previews = {}
        self.pin_bar = QFrame(chat)
        self.pin_bar.setObjectName('ChatPinnedMessages')
        apply_style(self.pin_bar, '#ChatPinnedMessages {background:transparent; border:none;}'
            '#ChatPinnedMessages QToolButton {background:@background.panel@; border:none; border-radius:6px; color:#a5b4fc; font-size:14px;}'
            '#ChatPinnedMessages QToolButton:hover {background:@control.hover@;}')
        pins = QVBoxLayout(self.pin_bar); pins.setContentsMargins(0, 0, 0, 0); pins.setSpacing(4)
        self.search_button = QToolButton(self.pin_bar)
        set_icon(self.search_button, 'search', size=18)
        set_text(self.search_button, 'ui.search_popup.search_180f', setter='setToolTip')
        self.search_button.setCursor(Qt.PointingHandCursor)
        pins.addWidget(self.search_button)
        self.pin_nav_button = QToolButton(self.pin_bar)
        set_text(self.pin_nav_button, 'ui.chat_references.go_to_next_pinned_message_544b', setter='setToolTip')
        set_text(self.pin_nav_button, 'ui.chat_references.go_to_pinned_message_1f1d', setter='setAccessibleName')
        self.pin_nav_button.setFocusPolicy(Qt.NoFocus); self.pin_nav_button.setCursor(Qt.PointingHandCursor)
        self.pin_nav_button.clicked.connect(self.advance_pin)
        self.pin_list_button = QToolButton(self.pin_bar); symbol_button(self.pin_list_button, '▼')
        set_text(self.pin_list_button, 'ui.chat_references.all_pinned_messages_8ab1', setter='setToolTip'); self.pin_list_button.setFocusPolicy(Qt.NoFocus)
        self.pin_list_button.setCursor(Qt.PointingHandCursor)
        self.pin_list_button.clicked.connect(self.show_pins)
        self._pin_side = None
        set_icon(self.pin_nav_button, 'pin', size=18)
        pins.addWidget(self.pin_nav_button); pins.addWidget(self.pin_list_button)
        self.pin_nav_button.hide()
        self.pin_list_button.hide()
        self.pin_bar.hide()
        self.reply_bar = QFrame(chat.center_col)
        self.reply_bar.setObjectName('ChatReplyPreview')
        apply_style(self.reply_bar, '#ChatReplyPreview {background:@background.panel@; border:1px solid @border.subtle@; border-left:2px solid @accent.primary@; border-radius:6px;}')
        reply = QHBoxLayout(self.reply_bar); reply.setContentsMargins(4, 2, 4, 2)
        self.reply_preview = PreviewButton(self.reply_bar, tooltip=False); self.reply_preview.clicked.connect(self.jump_pending)
        close = QToolButton(self.reply_bar); set_icon(close, 'close', square=True); close.setFocusPolicy(Qt.NoFocus)
        square_button(close)
        self.reply_close_button = close
        set_text(close, 'ui.chat_references.cancel_reply_2a3b', setter='setToolTip'); close.clicked.connect(self.clear_reply)
        reply.addWidget(self.reply_preview, 1); reply.addWidget(close)
        self.reply_bar.hide()
        self.quote_hint = QFrame(chat.center_col)
        self.quote_hint.setObjectName('ChatQuoteHint')
        apply_style(self.quote_hint, '#ChatQuoteHint {background:@background.panel@; border:1px solid @border.subtle@; border-radius:6px;} #ChatQuoteHint QLabel {background:transparent; color:@text.secondary@;}')
        hint_layout = QHBoxLayout(self.quote_hint)
        hint_layout.setContentsMargins(8, 4, 8, 4)
        hint_label = localize(QLabel(self.quote_hint), 'chat.quotes.selection_hint')
        hint_label.setAlignment(Qt.AlignCenter)
        hint_label.setWordWrap(False)
        hint_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        hint_layout.addWidget(hint_label)
        self.quote_hint.setFixedHeight(hint_label.fontMetrics().lineSpacing() + 10)
        self.quote_hint.hide()
        chat.input_edit.quotes_changed.connect(self._quotes_changed)
        self.position_pending = False
        QApplication.instance().installEventFilter(self)

    def update_search_visibility(self):
        started = self.chat.welcome_widget.isHidden() and not self.chat.scroll_area.isHidden()
        self.search_button.setVisible(started)
        self.pin_bar.setVisible(started)
        if not started:
            search = getattr(self.chat,'chat_search',None)
            if search is not None:search.popup.close_popup()
        self.schedule_position()

    def schedule_position(self):
        if not self.position_pending:
            self.position_pending = True
            defer(0, self, self.position_overlays)

    def _quotes_changed(self):
        self.quote_hint.setVisible(bool(self.pending and self.chat.input_edit.quote_objects()))
        self.schedule_position()

    def reply_overlay_top(self):
        return self.quote_hint.y() if self.quote_hint.isVisible() else self.reply_bar.y()

    def position_overlays(self):
        self.position_pending = False
        if not isValid(self.chat) or not isValid(self.pin_bar):
            return
        viewport = self.chat.scroll_area.viewport()
        origin = viewport.mapTo(self.chat.center_col, QPoint(0, 0))
        top = bottom = 0
        if self.pin_bar.isVisible():
            left_edge = viewport.mapTo(self.chat, QPoint(0, 0))
            side = min(40, max(1, left_edge.x()))
            gap = min(8, max(0, left_edge.x() - side))
            if side != self._pin_side:
                for button in (self.search_button, self.pin_nav_button, self.pin_list_button):
                    square_button(button, side)
                self.pin_nav_button.setIconSize(QSize(min(18, side), min(18, side)))
                self._pin_side = side
            count = 1 + (2 if not self.pin_nav_button.isHidden() else 0)
            self.pin_bar.setGeometry(left_edge.x() - side - gap, left_edge.y(), side, side * count + 4 * (count - 1))
            self.pin_bar.raise_()
        if self.reply_bar.isVisible():
            height = self.reply_bar.sizeHint().height()
            composer = self.chat._composer_rect()
            self.reply_bar.setGeometry(composer.x(), composer.y() - 4 - height,
                                       composer.width(), height)
            self.reply_bar.raise_()
            if self.quote_hint.isVisible():
                self.quote_hint.setGeometry(composer.x(), self.reply_bar.y() - self.quote_hint.height() - 4,
                                            composer.width(), self.quote_hint.height())
                self.quote_hint.raise_()
            bottom = max(0, origin.y() + viewport.height() - self.reply_overlay_top()) + 8
        margins = self.chat.turns_layout.contentsMargins()
        if (margins.top(), margins.bottom()) != (top, bottom):
            # These are scrollable gaps, not reserved viewport space. Both
            # edge messages can be scrolled fully clear of the overlays.
            bar = self.chat.scroll_area.verticalScrollBar()
            value = bar.value()
            delta = top - margins.top()
            preserve_position = bool(delta and bar.maximum() and not self.chat._auto_scroll_to_bottom)
            self.chat.turns_layout.setContentsMargins(0, top, 0, bottom)
            if preserve_position:
                def restore_position():
                    if isValid(self.chat) and isValid(bar) and bar.value() == value:
                        bar.setValue(value + delta)
                        self.chat._auto_scroll_to_bottom = False
                defer(0, self, restore_position)
        for position in (self.chat._position_attachments_panel, self.chat._position_error_panel,
                         self.chat._position_tags_bar):
            position()
        search = getattr(self.chat,'chat_search',None)
        if search is not None and search.popup.isVisible():search.popup.raise_()

    def turns(self):
        return self.chat.message_selection.turns()

    def lookup(self, identity):
        turns = self.turns()
        key = (self.chat.message_selection._turns_key, self._identity_revision)
        if self._turn_index_key != key:
            index = {}
            for turn in turns:
                # Preserve lookup's first-match behavior for legacy histories
                # that contain duplicate ids. Metadata exports still deep-copy.
                if not hasattr(turn, 'chat_meta'):
                    turn_metadata(turn)
                index.setdefault(turn.chat_meta[CHAT_ID], turn)
            self._turn_index = index
            self._turn_index_key = key
        return self._turn_index.get(identity)

    def invalidate_turn_index(self):
        self._identity_revision += 1
        self._turn_index_key = None

    def can_reply(self, turn):
        from ui.views.chat_view import UserMessageTurn, AssistantMessageTurn
        return isinstance(turn, (UserMessageTurn, AssistantMessageTurn))

    def author(self, turn):
        from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn
        if getattr(turn,'is_subagent_block',False):return tr('ui.subagents_panel.subagents_cf58')
        return tr('ui.caption.041bfceecc8f') if isinstance(turn, UserMessageTurn) else (tr('ui.chat_view.model_reasoning_a157') if isinstance(turn, ThoughtMessageTurn) else 'Toshy Agent')

    def text(self, turn):
        from ui.views.chat_selection import message_markdown
        _, markdown = message_markdown(turn)
        source = markdown[:2000]
        cached = self._plain_previews.get(turn)
        if cached is not None and cached[0] == source:
            return cached[1]
        document = QTextDocument(); document.setMarkdown(source)
        plain = document.toPlainText()
        self._plain_previews[turn] = (source, plain)
        return plain

    def selected_text(self, turn, preferred=None):
        view = getattr(turn, 'md_view', None)
        if view is None:
            return '', None
        widgets = view.findChildren(QLabel) + view.findChildren(QTextEdit)
        candidates = [preferred, self.last_selection_widget] + widgets
        for widget in candidates:
            if widget is None or not isValid(widget) or widget not in widgets or not widget.isVisible():
                continue
            if isinstance(widget, QLabel):
                text = widget.selectedText().replace('\u2029', '\n').replace('\u2028', '\n')
                if text:
                    return text, (widget, widget.selectionStart(), len(text.encode('utf-16-le')) // 2)
            elif isinstance(widget, QTextEdit):
                cursor = widget.textCursor()
                if cursor.hasSelection():
                    # Qt's paragraph separators represent actual selected line breaks.
                    return cursor.selectedText().replace('\u2029', '\n').replace('\u2028', '\n'), (widget, cursor)
        return '', None

    def prepare_reply(self, turn, quote=None, selection=None):
        if not self.can_reply(turn) or self.chat._preparing_attachments:
            return
        if quote is None:
            quote, selection = self.selected_text(turn)
        target_id = turn_metadata(turn)[CHAT_ID]
        if self.chat.input_edit.quote_objects():
            if self.pending and self.pending['target'] == target_id and selection:
                self.quote_selection.toggle(turn, selection[0])
            return
        self.quote_selection.clear()
        data = {'target': target_id, 'author': self.author(turn),
                'preview': self.text(turn)[:200]}
        if quote:
            data['quote'] = quote  # Exact snapshot, with no strip, clipping or summarization.
        self.pending = data
        if hasattr(self.chat,"input_state"):self.chat.input_state.draft_changed()
        if quote:
            self.quote_selection.add_initial(target_id, quote, selection)
        self.refresh()
        self.chat.input_edit.setFocus()
        if selection is not None and isValid(selection[0]):
            if isinstance(selection[0], QLabel):
                selection[0].setSelection(selection[1], selection[2])
            else:
                selection[0].setTextCursor(selection[1])

    def clear_reply(self):
        self.pending = None
        if hasattr(self.chat,"input_state"):self.chat.input_state.draft_changed()
        self.quote_selection.clear()
        self.reply_bar.hide()
        self.quote_hint.hide()
        self.schedule_position()

    def snapshot_reply(self):
        data = deepcopy(self.pending)
        if data is not None:
            data.pop('quote', None)
            quotes = self.chat.input_edit.quote_objects()
            if quotes:
                data['quotes'] = [item['text'] for item in quotes]
        return data

    def consume_reply(self, data):
        if self.snapshot_reply() == data:
            self.clear_reply()

    def jump(self, turn, align='center'):
        if turn is None or not isValid(turn):
            return
        self.position_overlays()
        self.chat._auto_scroll_to_bottom = False
        self.chat.message_selection.turns()
        from ui.views.chat_model_blocks import owner, members
        turn = owner(turn)
        block = members(turn)
        viewport = self.chat.scroll_area.viewport()
        top = 8
        bottom = (max(0, viewport.height() - self.reply_overlay_top()
                      + viewport.mapTo(self.chat.center_col, QPoint()).y()) + 8
                  if self.reply_bar.isVisible() else 8)
        point = block[0].mapTo(viewport, QPoint(0, 0))
        height = block[-1].mapTo(viewport, QPoint(0, 0)).y() + block[-1].height() - point.y()
        available = max(1, viewport.height() - top - bottom)
        destination = top if align == 'top' else top + max(0, (available - height) // 2)
        bar = self.chat.scroll_area.verticalScrollBar()
        bar.setValue(bar.value() + point.y() - destination)
        self.chat._auto_scroll_to_bottom = False
        flash = getattr(turn, 'navigation_flash', None)
        if flash is not None:
            flash.animation.stop()
            flash.animation.start()
        else:
            turn.navigation_flash = MessageFlash(turn, self.chat.message_selection)
            turn.navigation_flash.animation.start()

    def jump_pending(self):
        if self.pending:
            self.jump(self.lookup(self.pending['target']))

    def submit_pin(self, key):
        if key not in self.pin_updates or key in self.pin_inflight:
            return
        self.pin_inflight.add(key)
        session, branch, identity = key
        desired = self.pin_updates[key]
        def done(_result):
            self.pin_inflight.discard(key)
            if self.pin_updates.get(key) == desired:
                self.pin_updates.pop(key, None)
            else:
                self.submit_pin(key)
        def failed(error):
            self.pin_inflight.discard(key)
            if self.pin_updates.get(key) != desired:
                self.submit_pin(key)
                return
            status = getattr(getattr(error, 'response', None), 'status_code', None)
            if status == 409 or status is None:
                defer(1000, self, lambda: self.submit_pin(key) if isValid(self.chat) else None)
                return
            self.pin_updates.pop(key, None)
            if (self.chat.current_session_id, self.chat.current_branch) == (session, branch):
                self.chat._show_error_panel(tr('ui.caption.048b09c6c113') + str(error))
        self.chat.session_write_runner.submit(
            lambda: self.chat.session_manager.set_message_pin(session, identity, desired, branch),
            done, failed)

    def toggle_pin(self, turn):
        turn_metadata(turn)
        turn.chat_meta[PINNED] = not turn.chat_meta[PINNED]
        if turn.chat_meta[PINNED]: self.next_pin = turn.chat_meta[CHAT_ID]
        if self.chat.current_session_id:
            key = (self.chat.current_session_id, self.chat.current_branch, turn.chat_meta[CHAT_ID])
            self.pin_updates[key] = turn.chat_meta[PINNED]
            self.submit_pin(key)
        self.refresh()

    def pinned(self):
        return [turn for turn in self.turns() if turn_metadata(turn)[PINNED]]

    def advance_pin(self):
        pins = self.pinned()
        if not pins: return
        index = next((i for i, turn in enumerate(pins) if turn_metadata(turn)[CHAT_ID] == self.next_pin), len(pins) - 1)
        self.jump(pins[index])
        self.next_pin = turn_metadata(pins[(index - 1) % len(pins)])[CHAT_ID]
        self.refresh()

    def select_pin(self, identity):
        pins = self.pinned()
        turn = self.lookup(identity)
        if turn not in pins: return
        self.jump(turn)
        self.next_pin = turn_metadata(pins[(pins.index(turn) - 1) % len(pins)])[CHAT_ID]
        self.refresh()

    def show_pins(self):
        menu = QMenu(self.chat); menu.setObjectName('ChatMessageContextMenu')
        for turn in reversed(self.pinned()):
            button = PreviewButton(menu)
            button.setFixedWidth(max(240, min(620, self.chat.input_card.width())))
            button.preview(self.author(turn), self.text(turn))
            identity = turn_metadata(turn)[CHAT_ID]
            button.clicked.connect(lambda checked=False, target=identity: (menu.close(), self.select_pin(target)))
            action = QWidgetAction(menu); action.setDefaultWidget(button); add_caption_action(menu, 'addAction', action)
        self.pin_menu = menu
        menu.popup(self.pin_list_button.mapToGlobal(self.pin_list_button.rect().bottomLeft()))

    def extend_menu(self, turn, menu, preferred=None):
        if self.can_reply(turn):
            quote, selection = self.selected_text(turn, preferred)
            action = add_caption_action(menu, 'addAction', tr('ui.caption.9dfe403a50f4') if quote else tr('ui.caption.b9af23eeff5d'))
            action.triggered.connect(lambda: self.prepare_reply(turn, quote, selection))
        label = tr('ui.caption.09e33790a780') if turn_metadata(turn)[PINNED] else tr('ui.caption.4087d0c45e73')
        add_caption_action(menu, 'addAction', label).triggered.connect(lambda: self.toggle_pin(turn))
        menu.addSeparator()

    def attach_reply(self, turn):
        data = turn_metadata(turn).get(REPLY)
        if not isinstance(data, dict): return
        if not hasattr(turn, 'reply_link'):
            turn.reply_link = ReplyPreviewButton(turn)
            turn.main_layout.setSpacing(6)
            turn.main_layout.insertWidget(0, turn.reply_link)
            turn.reply_link.clicked.connect(lambda: self.jump(self.lookup(turn.chat_meta[REPLY].get('target'))))
        target = self.lookup(data.get('target'))
        text = data.get('quote') if 'quote' in data else (self.text(target) if target is not None else data.get('preview', ''))
        title = tr('ui.caption.cd7f0f3d67a5') + str(data.get('author') or tr('ui.inspector.message_2f77'))
        if target is None:
            title += tr('ui.caption.3e38e8b2d446') if data.get('source_session') else tr('ui.caption.ace40f5a1da3')
        turn.reply_link.preview(title, text)
        turn.reply_link.setEnabled(target is not None)

    def refresh(self):
        self.refresh_pending = False
        if getattr(self.chat, '_session_loading', False): return
        if not isValid(self.chat) or not isValid(self.pin_bar): return
        turns = self.turns()
        live = set(turns)
        self._plain_previews = {turn: value for turn, value in self._plain_previews.items() if turn in live}
        for turn in self.turns():
            key = (self.chat.current_session_id, self.chat.current_branch, turn_metadata(turn)[CHAT_ID])
            if key in self.pin_updates:
                turn.chat_meta[PINNED] = self.pin_updates[key]
        pins = self.pinned()
        self.update_search_visibility()
        self.pin_nav_button.setVisible(bool(pins))
        self.pin_list_button.setVisible(bool(pins))
        if pins:
            turn = next((t for t in pins if turn_metadata(t)[CHAT_ID] == self.next_pin), pins[-1])
            self.next_pin = turn_metadata(turn)[CHAT_ID]
        for turn in self.turns():
            self.attach_reply(turn)
        if self.pending:
            target = self.lookup(self.pending['target'])
            if target is None and not self.chat.input_edit.quote_objects():
                self.clear_reply()
            else:
                text = self.text(target) if target is not None else self.pending.get('preview', '')
                title = tr('ui.caption.cd7f0f3d67a5') + self.pending['author']
                self.reply_preview.preview(title, text)
                self.reply_bar.show()
                self.quote_hint.setVisible(bool(self.chat.input_edit.quote_objects()))
        self.position_overlays()

    def schedule_refresh(self):
        if getattr(self.chat, '_session_loading', False): return
        if not self.refresh_pending:
            self.refresh_pending = True
            defer(0, self, self.refresh)

    def reset(self):
        search = getattr(self.chat, 'chat_search', None)
        if search is not None:search.popup.close_popup()
        self.quote_selection.clear(forget_sources=True)
        self.clear_reply(); self.next_pin = None
        self.invalidate_turn_index()
        self._turn_index.clear()
        self._plain_previews.clear()
        self.last_selection_widget = None
        self.pin_nav_button.hide(); self.pin_list_button.hide()
        self.pin_bar.hide(); self.schedule_position()
        if hasattr(self, 'pin_menu'): self.pin_menu.close()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind not in _FILTER_EVENTS:
            return False
        if not isValid(self.chat): return False
        if (getattr(self.chat, '_session_loading', False)
                and kind in (QEvent.ContextMenu, QEvent.MouseButtonRelease)
                and isinstance(watched, QWidget)
                and self.chat.turns_container.isAncestorOf(watched)):
            return True
        if watched in (self.chat, self.chat.center_col, self.chat.scroll_area, self.chat.scroll_area.viewport(),
                       self.chat.input_card, self.chat.message_selection.toolbar_holder,
                       self.pin_bar, self.reply_bar) and kind in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide):
            self.schedule_position()
        if watched is self.chat.turns_container and kind in (QEvent.LayoutRequest, QEvent.ChildAdded, QEvent.ChildRemoved):
            self.schedule_refresh()
        if (isinstance(watched, QWidget) and kind in (QEvent.ContextMenu, QEvent.MouseButtonRelease)
                and self.chat.isAncestorOf(watched)):
            self.turns()
            turns = self.chat.message_selection._turn_members; turn = watched
            while turn is not None and turn not in turns: turn = turn.parentWidget()
            if turn is None: return False
            if kind == QEvent.MouseButtonRelease:
                widget = watched
                if not isinstance(widget, (QLabel, QTextEdit)) and isinstance(widget.parentWidget(), QTextEdit):
                    widget = widget.parentWidget()
                if event.button() == Qt.LeftButton and isinstance(widget, (QLabel, QTextEdit)):
                    self.last_selection_widget = widget
                    if self.chat.input_edit.quote_objects() and not self.chat.message_selection.active:
                        defer(0, self, lambda t=turn, w=widget:
                            self.quote_selection.toggle(t, w) if isValid(t) and isValid(w) else None)
                return False
            if self.chat.message_selection.active or turn.is_editing: return False
            menu = watched.createStandardContextMenu() if hasattr(watched, 'createStandardContextMenu') else QMenu(turn)
            menu.setObjectName('ChatMessageContextMenu')
            self.extend_menu(turn, menu, watched)
            if not menu.actions() or not hasattr(watched, 'createStandardContextMenu'):
                from ui.views.chat_selection import copy_from_menu, select_from_menu
                quote, _ = self.selected_text(turn, watched)
                localize(add_caption_action(menu, 'addAction', 'Копировать текст'), 'ui.chat_references.copy_text_3cbc').triggered.connect(
                    lambda: QApplication.clipboard().setText(quote) if quote else copy_from_menu(turn, False))
                localize(add_caption_action(menu, 'addAction', 'Выделить сообщение'), 'ui.chat_references.select_message_e129').triggered.connect(lambda: select_from_menu(turn))
            self.context_menu = menu
            menu.popup(event.globalPos()); event.accept(); return True
        return False
