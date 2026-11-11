from ui.themes.manager import theme_manager
"""Bounded terminal scrollback, protected inline prompt and command timeline."""
from ui.i18n import add_caption_action, bind_caption, tr, localize

from functools import wraps
from PySide6.QtCore import Qt, Signal, QPointF, QRectF, QSize
from PySide6.QtGui import (QColor, QPainter, QPen, QPixmap, QIcon, QTextCursor,
                          QTextCharFormat, QTextBlockUserData)
from PySide6.QtWidgets import QPlainTextEdit, QWidget, QComboBox, QMenu, QToolTip


def document_edit(method):
    """Keep the input bookmark independent of intermediate output edits."""
    @wraps(method)
    def edit(self, *args, **kwargs):
        previous = self._syncing_cursor
        self._syncing_cursor = True
        try:
            return method(self, *args, **kwargs)
        finally:
            self._syncing_cursor = previous
            if not previous:
                self._sync_input_cursor()
    return edit


def mode_icon(settings=False):
    pixmap = QPixmap(20, 20)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor('#b9becb'), 1.5))
    if settings:
        painter.translate(10, 10)
        for _ in range(8):
            painter.drawLine(QPointF(0, -6), QPointF(0, -8))
            painter.rotate(45)
        painter.drawEllipse(QPointF(0, 0), 5.5, 5.5)
        painter.drawEllipse(QPointF(0, 0), 2, 2)
    else:
        painter.drawRoundedRect(QRectF(1.5, 3, 17, 14), 2, 2)
        painter.drawLine(QPointF(5, 7), QPointF(8, 10))
        painter.drawLine(QPointF(8, 10), QPointF(5, 13))
        painter.drawLine(QPointF(11, 13), QPointF(15, 13))
    painter.end()
    return QIcon(pixmap)


class ContainerSelector(QComboBox):
    COLORS = {'not_created': '#71717a', 'stopped': '#71717a', 'running': '#4ade80',
              'busy': '#facc15', 'error': '#f87171'}

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = 'stopped'
        self.setStyleSheet('QComboBox {padding-right:36px;}')

    def set_state(self, state, detail=''):
        self.state = state
        names = {'not_created': tr('ui.caption.5f317b01bca2'), 'stopped': tr('ui.caption.5f317b01bca2'),
                 'running': tr('ui.caption.f15350f67f61'), 'busy': tr('ui.caption.0d1d04fd3efa'), 'error': tr('ui.caption.f8e15fcf9682')}
        bind_caption(self, names.get(state, names['stopped']) + ('\n' + detail if detail else '') +
                        tr('ui.caption.8cda108c5ea8'), setter='setToolTip')
        self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(self.COLORS.get(self.state, '#71717a')))
        painter.drawEllipse(QPointF(self.width() - 22, self.height() / 2), 4, 4)


class CommandMark(QTextBlockUserData):
    def __init__(self, identity, command):
        super().__init__()
        self.identity, self.command = identity, command


class TerminalConsole(QPlainTextEdit):
    command_requested = Signal(str)
    cancel_requested = Signal()
    markers_changed = Signal()
    completion_requested = Signal(str, int)
    process_key_requested = Signal(str)

    def __init__(self, parent=None, lines=10000):
        super().__init__(parent)
        self.setObjectName('TerminalFeed')
        self._text_boundary_in_key_handler = True
        self.setStyleSheet('QPlainTextEdit#TerminalFeed {background:#141418;color:#d6d6df;'
                          'border:none;border-radius:5px;padding:5px;font-family:monospace;font-size:12px;}')
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setUndoRedoEnabled(False)
        self.setAcceptDrops(True)
        self.line_limit = lines
        self.directory = ''
        self.prompt_visible = True
        self._hidden_draft = ''
        self.allow_interrupt = False
        self.process_mode = False
        self.target, self.backend = 'docker', 'docker'
        self._syncing_cursor = False
        self._selecting_output = False
        self._input_cursor = None
        self._horizontal_anchor = 0
        self._restoring_horizontal = False
        self._allow_horizontal_follow = False
        horizontal = self.horizontalScrollBar()
        horizontal.actionTriggered.connect(lambda _: self._horizontal_action())
        horizontal.sliderMoved.connect(self._horizontal_action)
        horizontal.valueChanged.connect(self._horizontal_changed)
        horizontal.rangeChanged.connect(self._horizontal_range_changed)
        self.entries, self.history = {}, []
        self.history_index, self.saved_draft = 0, ''
        self._normal = QTextCharFormat()
        self._normal.setForeground(QColor('#d6d6df'))
        self._sender = QTextCharFormat()
        self._sender.setForeground(QColor('#4ade80'))
        self._error = QTextCharFormat()
        self._error.setForeground(QColor('#f87171'))
        self.reset_feed()
        self.cursorPositionChanged.connect(self._sync_input_cursor)
        self.selectionChanged.connect(self._sync_input_cursor)

    @document_edit
    def reset_feed(self):
        self._horizontal_anchor = 0
        self.clear()
        self.entries = {}
        self._hidden_draft = ''
        cursor = self.textCursor()
        self.prompt = QTextCursor(cursor)
        self.prompt.setKeepPositionOnInsert(True)
        if self.prompt_visible:self._write_prompt(cursor, 'user', self.directory, self.target, self.backend)
        self.prompt.setKeepPositionOnInsert(False)
        self.input_start = QTextCursor(cursor)
        self.input_start.setKeepPositionOnInsert(True)
        self.setTextCursor(cursor)
        self._input_cursor = QTextCursor(cursor)
        self.markers_changed.emit()

    def _write_prompt(self, cursor, sender, directory, target=None, backend=None):
        cursor.insertText(sender, self._sender)
        cursor.insertText('@', self._normal)
        target_format = QTextCharFormat(self._normal)
        if (backend or self.backend) == 'local':
            target_format.setForeground(QColor('#facc15'))
        target = ''.join(c if c.isprintable() else '�' for c in str(target or self.target))
        cursor.insertText(target, target_format)
        # Paths are plain text, never markup or terminal control sequences.
        path = ''.join(c if c.isprintable() else '�' for c in str(directory or ''))
        cursor.insertText(':' + path + '$ ', self._normal)

    def set_environment(self, target, backend):
        if (self.target, self.backend) != (target, backend):
            self.target, self.backend = target, backend
            self.set_directory(self.directory, force=True)

    @document_edit
    def set_prompt_visible(self, visible):
        visible=bool(visible)
        if visible==self.prompt_visible:return
        bar=self.verticalScrollBar();position=bar.value();follow=position>=bar.maximum()-2
        start=self.prompt.position()
        cursor=self.cursor_at(self.prompt)
        if visible:
            self._write_prompt(cursor,'user',self.directory,self.target,self.backend)
            self.input_start.setPosition(cursor.position())
            cursor.insertText(self._hidden_draft,self._normal)
        else:
            self._hidden_draft=self.draft()
            cursor.movePosition(QTextCursor.End,QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
            self.input_start.setPosition(start)
        self.prompt_visible=visible
        self.prompt.setPosition(start)
        self.input_start.setKeepPositionOnInsert(True)
        self.setTextCursor(cursor);self._input_cursor=QTextCursor(cursor)
        bar.setValue(bar.maximum() if follow else position)

    @document_edit
    def set_directory(self, directory, force=False):
        directory = str(directory or '')
        if directory == self.directory and not force:
            return
        self.directory = directory
        if not self.prompt_visible:return
        bar = self.verticalScrollBar()
        position, follow = bar.value(), bar.value() >= bar.maximum() - 2
        before = self.textCursor()
        start = self.prompt.position()
        cursor = self.cursor_at(self.prompt)
        cursor.setPosition(self.input_start.position(), QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
        self._write_prompt(cursor, 'user', directory, self.target, self.backend)
        self.prompt.setPosition(start)
        self.input_start.setPosition(cursor.position())
        self.input_start.setKeepPositionOnInsert(True)
        self.setTextCursor(before)
        bar.setValue(bar.maximum() if follow else position)

    def draft(self):
        if not self.prompt_visible:return self._hidden_draft
        cursor = self.cursor_at(self.input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        return cursor.selectedText().replace('\u2029', '\n').replace('\u2028', '\n')

    @document_edit
    def set_draft(self, text):
        if not self.prompt_visible:
            self._hidden_draft=text
            return
        cursor = self.cursor_at(self.input_start)
        cursor.movePosition(QTextCursor.End, QTextCursor.KeepAnchor)
        cursor.insertText(text, self._normal)
        self.setTextCursor(cursor)
        self._input_cursor = QTextCursor(cursor)
        if not text:
            self._horizontal_anchor = 0
            self._horizontal_changed(self.horizontalScrollBar().value())

    def cursor_at(self, anchor):
        # Persistent Qt cursors can acquire a selection when text is inserted at
        # their position. Anchors here describe positions, never edit selections.
        cursor = QTextCursor(self.document())
        cursor.setPosition(anchor.position())
        return cursor

    def remember(self, command):
        if not self.history or self.history[-1] != command:
            self.history.append(command)
            self.history = self.history[-500:]
        self.history_index = len(self.history)
        self.saved_draft = ''

    def set_input_enabled(self, enabled, reason=''):
        self.setReadOnly(not enabled)
        bind_caption(self, reason, setter='setToolTip')

    def _horizontal_action(self, position=None):
        bar = self.horizontalScrollBar()
        self._horizontal_anchor = bar.sliderPosition() if position is None else int(position)

    def _horizontal_range_changed(self, minimum, maximum):
        self._horizontal_anchor = max(minimum, min(self._horizontal_anchor, maximum))
        self._horizontal_changed(self.horizontalScrollBar().value())

    def _horizontal_changed(self, value):
        if self._restoring_horizontal:
            return
        bar = self.horizontalScrollBar()
        if self._allow_horizontal_follow or bar.isSliderDown():
            self._horizontal_anchor = value
            return
        target = max(bar.minimum(), min(self._horizontal_anchor, bar.maximum()))
        if value != target:
            self._restoring_horizontal = True
            try:
                bar.setValue(target)
            finally:
                self._restoring_horizontal = False

    def _restore_input_cursor(self, preserve_view=False):
        if self._input_cursor is None:
            return
        vertical = self.verticalScrollBar()
        position = vertical.value()
        cursor = self.cursor_at(self._input_cursor)
        if cursor.position() < self.input_start.position():
            cursor.setPosition(self.input_start.position())
        self._syncing_cursor = True
        try:
            self.setTextCursor(cursor)
        finally:
            self._syncing_cursor = False
        if preserve_view:
            vertical.setValue(position)
        self.setCursorWidth(1)

    def _sync_input_cursor(self):
        if self._syncing_cursor or not hasattr(self, 'input_start'):
            return
        cursor = self.textCursor()
        if cursor.selectionStart() >= self.input_start.position():
            self._input_cursor = QTextCursor(cursor)
            self._input_cursor.clearSelection()
            self.setCursorWidth(1)
        elif cursor.hasSelection() or self._selecting_output:
            # Native selection remains available for copying history. Its moving
            # end is not the terminal's editing caret.
            self.setCursorWidth(0)
        else:
            self._restore_input_cursor(preserve_view=True)
        self.viewport().update()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._selecting_output = True
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._selecting_output = True
        super().mouseDoubleClickEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._selecting_output = False
        self._sync_input_cursor()

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.cursorWidth() == 0 and self._input_cursor is not None and self.hasFocus() and not self.isReadOnly():
            rect = self.cursorRect(self.cursor_at(self._input_cursor))
            painter = QPainter(self.viewport())
            painter.fillRect(QRectF(rect.x(), rect.y(), 1, rect.height()), QColor('#d6d6df'))

    def wheelEvent(self, event):
        horizontal = bool(event.angleDelta().x() or event.pixelDelta().x() or event.modifiers() & Qt.ShiftModifier)
        self._allow_horizontal_follow = horizontal
        try:
            super().wheelEvent(event)
        finally:
            self._allow_horizontal_follow = False

    def _end_anchor(self, cursor):
        result = QTextCursor(cursor)
        result.setKeepPositionOnInsert(True)
        return result

    def _entry(self, job):
        existing = self.entries.get(job['id'])
        sender = 'user' if job.get('source') == 'manual' else 'toshy'
        # Replacing a shell's whole screen can delete its first QTextBlock and
        # its userData. The retained range cursors still delimit that screen;
        # treating the lost block mark as a new entry appends the history again.
        if existing and (existing.get('terminal_shell') or
                         (existing['block'].isValid() and isinstance(existing['block'].userData(), CommandMark))):
            if existing['cwd'] != job.get('cwd', '') and not job.get('terminal_shell'):
                cursor = QTextCursor(existing['block'])
                cursor.setPosition(existing['header_end'].position(), QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                self._write_prompt(cursor, sender, job.get('cwd', ''), job.get('target', 'docker'), job.get('backend', 'docker'))
                existing['header_end'].setPosition(cursor.position())
                existing['cwd'] = job.get('cwd', '')
            return existing
        cursor = self.cursor_at(self.prompt)
        block = cursor.block()
        if not job.get('terminal_shell'):
            self._write_prompt(cursor, sender, job.get('cwd', ''), job.get('target', 'docker'), job.get('backend', 'docker'))
        header_end = self._end_anchor(cursor)
        if not job.get('terminal_shell'):cursor.insertText(job['command'] + '\n', self._normal)
        block.setUserData(CommandMark(job['id'], job['command']))
        end = self._end_anchor(cursor)
        entry = {'block': block, 'end': end, 'reported': False,
                 'header_end': header_end, 'output_start':self._end_anchor(cursor), 'cwd': job.get('cwd', ''), 'separator': False,
                 'terminal_shell':bool(job.get('terminal_shell'))}
        self.entries[job['id']] = entry
        return entry

    @document_edit
    def apply_records(self, records):
        bar = self.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 2
        first = self.firstVisibleBlock()
        cursor_before = self.textCursor()
        for record in records:
            job = record['job']
            entry = self._entry(job)
            if job.get('tty') and record.get('screen') is not None:
                # Replace the process's screen, keeping other command blocks and
                # the protected input draft intact. Raw VT output stays on disk.
                snapshot=record['screen']['text']
                if entry.get('last_screen')!=snapshot:
                    cursor=self.cursor_at(entry['output_start'])
                    cursor.setPosition(entry['end'].position(),QTextCursor.KeepAnchor)
                    if entry['separator']:cursor.movePosition(QTextCursor.Right,QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                    cursor.insertText(snapshot+('' if snapshot.endswith('\n') or not snapshot else '\n'),self._normal)
                    entry['end'].setPosition(cursor.position());entry['separator']=False
                    entry['last_screen']=snapshot;entry['reported']=False
            elif record['text']:
                cursor = self.cursor_at(entry['end'])
                if entry['separator']:
                    cursor.deleteChar()
                    entry['separator'] = False
                text = record['text'].replace('\r\n','\n').replace('\r','')
                if text.count('\n') > self.line_limit:
                    text = ''.join(text.splitlines(keepends=True)[-self.line_limit:])
                cursor.insertText(text, self._normal)
                entry['end'].setPosition(cursor.position())
                if not text.endswith('\n'):
                    cursor.insertText('\n', self._normal)
                    entry['separator'] = True
            if not entry['reported'] and job.get('status') in {'failed', 'cancelled', 'timed_out', 'interrupted'} and record['offset'] >= record['size']:
                label = {'failed': tr('ui.caption.b455c916a5ad'), 'cancelled': tr('ui.caption.318c1f2d5d51'),
                         'timed_out': tr('ui.caption.3c8f89c44678'), 'interrupted': tr('ui.caption.8fe19a6e4015')}[job['status']]
                if job.get('exit_code') is not None:
                    label += tr('ui.caption.f96357f3a815', v0=job['exit_code'])
                cursor = self.cursor_at(entry['end'])
                if entry['separator']:
                    cursor.deleteChar(); entry['separator'] = False
                cursor.insertText(('' if cursor.atBlockStart() else '\n') + '[' + label + ']\n', self._error)
                entry['end'].setPosition(cursor.position())
                entry['reported'] = True
            self.trim_history()
        self.trim_history()
        self.setTextCursor(cursor_before)
        if follow:
            bar.setValue(bar.maximum())
        elif first.isValid():
            bar.setValue(first.blockNumber())
        self.markers_changed.emit()

    @document_edit
    def trim_history(self):
        excess = self.prompt.blockNumber() - self.line_limit
        cutoff = self.document().findBlockByNumber(excess).position() if excess > 0 else 0
        # Also bound pathological single-line output. Disk transcripts remain complete.
        cutoff = max(cutoff, self.prompt.position() - 2 * 1024 * 1024)
        if cutoff > 0:
            char = self.document().characterAt(cutoff)
            if char and 0xDC00 <= ord(char) <= 0xDFFF:
                cutoff += 1
            cursor = QTextCursor(self.document())
            cursor.setPosition(0)
            cursor.setPosition(cutoff, QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        self.entries = {key: entry for key, entry in self.entries.items()
                        if entry.get('terminal_shell') or entry['block'].isValid()}

    def set_line_limit(self, value):
        self.line_limit = int(value)
        self.trim_history()
        self.markers_changed.emit()

    def markers(self):
        result = []
        for entry in self.entries.values():
            block = entry['block']
            if block.isValid() and isinstance(block.userData(), CommandMark):
                result.append((block.blockNumber(), block.userData().command))
        return sorted(result)

    def _editable(self):
        cursor = self.textCursor()
        if cursor.selectionStart() < self.input_start.position():
            self._restore_input_cursor()

    def insertFromMimeData(self, source):
        if self.isReadOnly() or not source.hasText():
            return
        self._editable()
        self.textCursor().insertText(source.text(), self._normal)

    def inputMethodEvent(self, event):
        if not self.isReadOnly():
            self._editable()
        super().inputMethodEvent(event)

    def contextMenuEvent(self, event):
        menu = QMenu(self)
        copy = localize(add_caption_action(menu, 'addAction', 'Копировать', self.copy), 'ui.console.copy_1ecd')
        copy.setEnabled(self.textCursor().hasSelection())
        paste = localize(add_caption_action(menu, 'addAction', 'Вставить', self.paste), 'ui.console.paste_ec79')
        paste.setEnabled(not self.isReadOnly())
        localize(add_caption_action(menu, 'addAction', 'Выделить всё', self.selectAll), 'ui.console.select_all_694d')
        menu.exec(event.globalPos())

    def dropEvent(self, event):
        self.insertFromMimeData(event.mimeData())
        event.acceptProposedAction()

    def keyPressEvent(self, event):
        key, modifiers = event.key(), event.modifiers()
        control = bool(modifiers & Qt.ControlModifier)
        if control and key == Qt.Key_C:
            if self.textCursor().hasSelection():
                self.copy()
            else:
                if not self.isReadOnly() or self.allow_interrupt:
                    if self.process_mode:self.process_key_requested.emit('ctrl+c')
                    else:self.cancel_requested.emit()
            event.accept()
            return
        if self.isReadOnly():
            super().keyPressEvent(event)
            return
        if self.process_mode:
            special={Qt.Key_Tab:'tab',Qt.Key_Escape:'escape',Qt.Key_Up:'up',Qt.Key_Down:'down',
                     Qt.Key_Left:'left',Qt.Key_Right:'right',Qt.Key_Home:'home',Qt.Key_End:'end'}
            special.update({getattr(Qt,'Key_F'+str(n)):'f'+str(n) for n in range(1,13)})
            if control and key in (Qt.Key_D,Qt.Key_Z):
                self.process_key_requested.emit('ctrl+d' if key==Qt.Key_D else 'ctrl+z');event.accept();return
            if not modifiers and (key in (Qt.Key_Tab,Qt.Key_Escape) or not self.draft() and key in special):
                self.process_key_requested.emit(special[key]);event.accept();return
        if key == Qt.Key_Tab and not modifiers:
            self._editable()
            cursor = self.cursor_at(self.input_start)
            cursor.setPosition(self.textCursor().position(), QTextCursor.KeepAnchor)
            prefix = cursor.selectedText().replace('\u2029', '\n')
            self.completion_requested.emit(self.draft(), len(prefix))
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and modifiers & Qt.ShiftModifier:
            self._editable()
            self.textCursor().insertText('\n', self._normal)
            event.accept()
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not modifiers & Qt.ShiftModifier:
            command = self.draft()
            if command.strip() or self.process_mode:
                self.command_requested.emit(command)
            event.accept()
            return
        cursor = self.textCursor()
        if key in (Qt.Key_PageUp, Qt.Key_PageDown) and not modifiers:
            bar = self.verticalScrollBar()
            bar.setValue(bar.value() + (-bar.pageStep() if key == Qt.Key_PageUp else bar.pageStep()))
            return
        if control and key == Qt.Key_Home and not modifiers & Qt.ShiftModifier:
            self.verticalScrollBar().setValue(0)
            return
        if key in (Qt.Key_Up, Qt.Key_Down) and not modifiers:
            first = cursor.blockNumber() == self.input_start.blockNumber()
            last = cursor.blockNumber() == self.document().lastBlock().blockNumber()
            if cursor.position() >= self.input_start.position() and ((key == Qt.Key_Up and first) or (key == Qt.Key_Down and last)):
                if self.history_index == len(self.history):
                    self.saved_draft = self.draft()
                self.history_index = max(0, min(len(self.history), self.history_index + (-1 if key == Qt.Key_Up else 1)))
                self.set_draft(self.history[self.history_index] if self.history_index < len(self.history) else self.saved_draft)
                return
        if key in (Qt.Key_Backspace, Qt.Key_Delete):
            if cursor.selectionStart() < self.input_start.position() or (not cursor.hasSelection() and cursor.position() == self.input_start.position() and key == Qt.Key_Backspace):
                return
            if control and key == Qt.Key_Backspace and not cursor.hasSelection():
                end = cursor.position()
                cursor.movePosition(QTextCursor.PreviousWord)
                cursor.setPosition(max(cursor.position(), self.input_start.position()))
                cursor.setPosition(end, QTextCursor.KeepAnchor)
                cursor.removeSelectedText()
                return
        if control and key == Qt.Key_X:
            if cursor.selectionStart() >= self.input_start.position():
                self.cut()
            return
        if key == Qt.Key_Home and cursor.blockNumber() == self.input_start.blockNumber() and not control:
            cursor.setPosition(self.input_start.position(), QTextCursor.KeepAnchor if modifiers & Qt.ShiftModifier else QTextCursor.MoveAnchor)
            self.setTextCursor(cursor)
            return
        if event.text() and not control:
            self._editable()
        self._allow_horizontal_follow = cursor.selectionStart() >= self.input_start.position() and (
            bool(event.text() and not control) or key in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Backspace, Qt.Key_Delete))
        try:
            super().keyPressEvent(event)
        finally:
            self._allow_horizontal_follow = False


class TerminalRail(QWidget):
    def __init__(self, console, parent=None):
        super().__init__(parent)
        from ui.styles.binding import watch_paint
        watch_paint(self)
        self.console = console
        self.bar = console.verticalScrollBar()
        self.setFixedWidth(18)
        self.setMouseTracking(True)
        self.dragging, self.drag_offset, self.hover = False, 0, -1
        self.press_y = None
        self.bar.valueChanged.connect(lambda _: self.update())
        self.bar.rangeChanged.connect(lambda *_: self.update())
        console.markers_changed.connect(self.update)

    def geometry_values(self):
        height = float(self.height())
        maximum, page = self.bar.maximum(), self.bar.pageStep()
        thumb = height if maximum <= 0 else min(height, max(28, height * page / max(1, maximum + page)))
        top = (height - thumb) * self.bar.value() / max(1, maximum)
        return top, thumb

    def dots(self):
        count = len(self.console.markers())
        if count < 2:
            return [self.height() / 2] if count else []
        return [12 + i * max(0, self.height() - 24) / (count - 1) for i in range(count)]

    def hit(self, y):
        dots = self.dots()
        if not dots:
            return -1
        index = min(range(len(dots)), key=lambda i: abs(dots[i] - y))
        return index if abs(dots[index] - y) <= 7 else -1

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.drawLine(QPointF(9, 8), QPointF(9, self.height() - 8))
        top, thumb = self.geometry_values()
        painter.setPen(Qt.NoPen)
        painter.setBrush(theme_manager.color('accent.primary' if self.dragging or self.underMouse() else 'text.disabled'))
        painter.drawRoundedRect(QRectF(7, top, 4, thumb), 2, 2)
        for index, y in enumerate(self.dots()):
            radius = 5 if index == self.hover else 4
            painter.setBrush(theme_manager.color('accent.primary' if index == self.hover else 'text.disabled'))
            painter.drawEllipse(QPointF(9, y), radius, radius)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        y = event.position().y()
        self.press_y = y
        index = self.hit(y)
        if index >= 0:
            self.bar.setValue(self.console.markers()[index][0])
        else:
            top, thumb = self.geometry_values()
            self.dragging = True
            self.drag_offset = y - top if top <= y <= top + thumb else thumb / 2
            self.mouseMoveEvent(event)
        event.accept()

    def mouseMoveEvent(self, event):
        y = event.position().y()
        if not self.dragging and self.press_y is not None and event.buttons() & Qt.LeftButton and abs(y - self.press_y) > 3:
            self.dragging = True
            self.drag_offset = self.geometry_values()[1] / 2
        if self.dragging:
            _, thumb = self.geometry_values()
            self.bar.setValue(round(max(0, min(1, (y - self.drag_offset) / max(1, self.height() - thumb))) * self.bar.maximum()))
        else:
            self.hover = self.hit(y)
            if self.hover >= 0:
                QToolTip.showText(event.globalPosition().toPoint(), self.console.markers()[self.hover][1][:160], self)
            else:
                QToolTip.hideText()
        self.update()

    def mouseReleaseEvent(self, event):
        self.dragging = False
        self.press_y = None
        self.update()

    def leaveEvent(self, event):
        self.hover = -1
        QToolTip.hideText()
        self.update()

    def wheelEvent(self, event):
        delta = event.pixelDelta().y() / max(1, self.console.fontMetrics().height()) if event.pixelDelta().y() else event.angleDelta().y() / 40
        self.bar.setValue(self.bar.value() - round(delta))
        event.accept()
