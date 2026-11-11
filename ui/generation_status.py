"""Per-branch UI status, independent of which chat is currently visible."""
from ui.i18n import tr
import json
from pathlib import Path
from PySide6.QtCore import QObject, Signal, Qt, QRectF, QSaveFile, QIODevice
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QStyledItemDelegate, QStyleOptionViewItem, QToolTip, QStyle
from ui.event_bus import event_bus
from ui.app_state import app_state
from core.paths import APP_PATHS

STATES = {
    'running': ('#eab308', tr('ui.caption.61f5c7e1a025')),
    'finished': ('#22c55e', tr('ui.caption.ef05d57959cf')),
    'error': ('#ef4444', tr('ui.caption.cc5a2561f251')),
    'stopped': ('#f97316', tr('ui.caption.3028fa0907c0')),
}


class GenerationStatus(QObject):
    changed = Signal(str, str, str)

    def __init__(self, path=None):
        super().__init__()
        self.states = {}
        self.acknowledged = set()
        self.sequence = 0
        self.path = Path(path or APP_PATHS.runtime / 'generation_status.json')
        try:
            for item in json.loads(self.path.read_text(encoding='utf-8')):
                state = item['state']
                if state not in STATES or not item.get('session'):
                    continue
                # Viewing a finished answer acknowledges it across app restarts.
                if state == 'finished' and item.get('seen'):
                    continue
                self.sequence += 1
                if item.get('acknowledged') and state != 'running':
                    self.acknowledged.add((item['session'], item.get('branch') or 'main'))
                self.states[(item['session'], item.get('branch') or 'main')] = (
                    'stopped' if state == 'running' else state, self.sequence)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        self.view_index = 0
        self.active_chat = (app_state.session_id or '', app_state.branch)
        self.seen = set()
        event_bus.session_renamed.connect(self.rename_session)
        event_bus.branch_renamed.connect(self.rename_branch)
        app_state.session_changed.connect(self.select_chat)
        event_bus.workspace_view_changed.connect(self.select_view)
        from ui.core_bridge import core_process
        core_process.status_changed.connect(self.core_status_changed)
        self._save()

    def _save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            target = QSaveFile(str(self.path))
            if target.open(QIODevice.WriteOnly):
                payload = [{'session': sid, 'branch': branch, 'state': state,
                            'seen': (sid, branch) in self.seen, 'acknowledged': (sid, branch) in self.acknowledged}
                           for (sid, branch), (state, order) in sorted(self.states.items(), key=lambda item: item[1][1])]
                if target.write(json.dumps(payload, ensure_ascii=False).encode('utf-8')) >= 0:
                    target.commit()
        except OSError:
            pass

    def core_status_changed(self, online, key):
        if not online and key == 'core_status_offline':
            for (session, branch), (state, _) in list(self.states.items()):
                if state == 'running':
                    self.set(session, branch, 'stopped')

    def set(self, session, branch, state):
        if not session or state not in STATES:
            return
        self.sequence += 1
        self.states[(session, branch)] = (state, self.sequence)
        self.seen.discard((session, branch))
        self.acknowledged.discard((session, branch))
        self._mark_visible()
        self._save()
        # Every run transition is an event, even when another session has
        # already set the same colour. The header queues these events.
        self.changed.emit(session, branch, state)

    def acknowledge(self, keys):
        changed = []
        for key in keys:
            if key in self.states and key not in self.acknowledged:
                self.acknowledged.add(key)
                changed.append(key)
        if changed:
            self._save()
            for session, branch in changed:
                self.changed.emit(session, branch, '')

    def unread_state(self, session, branch=None):
        entries = [value for key, value in self.states.items()
                   if key not in self.acknowledged and key[0] == session and (branch is None or key[1] == branch)]
        if any(state == 'running' for state, _ in entries):
            return 'running'
        return max(entries, key=lambda value: value[1])[0] if entries else ''

    def _mark_visible(self):
        if self.view_index == 0 and self.state(*self.active_chat) == 'finished':
            changed = self.active_chat not in self.seen
            self.seen.add(self.active_chat)
            return changed
        return False

    def _leave_chat(self):
        if self.active_chat in self.seen and self.state(*self.active_chat) == 'finished':
            self.states.pop(self.active_chat, None)
            self.seen.discard(self.active_chat)
            self._save()
            self.changed.emit(*self.active_chat, '')
        self.seen.discard(self.active_chat)

    def select_chat(self, session, branch):
        key = (session or '', branch or 'main')
        if key != self.active_chat:
            self._leave_chat()
            self.active_chat = key
        if self._mark_visible():
            self._save()

    def select_view(self, index):
        if index != self.view_index:
            self._leave_chat()
            self.view_index = index
        if self._mark_visible():
            self._save()

    def state(self, session, branch=None):
        if branch is not None:
            return self.states.get((session, branch), ('', 0))[0]
        entries = [value for (sid, _), value in self.states.items() if sid == session]
        if any(state == 'running' for state, _ in entries):
            return 'running'
        return max(entries, key=lambda value: value[1])[0] if entries else ''

    def rename_session(self, old, new):
        for (session, branch), value in list(self.states.items()):
            if session == old:
                self.states[(new, branch)] = self.states.pop((session, branch))
        self.seen = {(new if sid == old else sid, branch) for sid, branch in self.seen}
        self.acknowledged = {(new if sid == old else sid, branch) for sid, branch in self.acknowledged}
        if self.active_chat[0] == old:
            self.active_chat = (new, self.active_chat[1])
        self._save()
        self.changed.emit(new, '', self.state(new))

    def rename_branch(self, session, old, new):
        value = self.states.pop((session, old), None)
        if value:
            self.states[(session, new)] = value
            if (session, old) in self.seen:
                self.seen.discard((session, old))
                self.seen.add((session, new))
            if (session, old) in self.acknowledged:
                self.acknowledged.discard((session, old))
                self.acknowledged.add((session, new))
            if self.active_chat == (session, old):
                self.active_chat = (session, new)
            self._save()
            self.changed.emit(session, new, value[0])


generation_status = GenerationStatus()


class SessionStatusDelegate(QStyledItemDelegate):
    def __init__(self, tree):
        super().__init__(tree)
        generation_status.changed.connect(self.refresh)

    def refresh(self, *_):
        self.parent().viewport().update()

    @staticmethod
    def status(index):
        data = index.data(Qt.UserRole) or {}
        if data.get('type') not in {'session', 'branch'}:
            return ''
        return generation_status.unread_state(data.get('session_id', ''),
            data.get('branch', 'main') if data.get('type') == 'branch' else None)

    def paint(self, painter, option, index):
        state = self.status(index)
        adjusted = QStyleOptionViewItem(option)
        self.initStyleOption(adjusted, index)
        if state:
            style = adjusted.widget.style()
            text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, adjusted, adjusted.widget)
            adjusted.text = adjusted.fontMetrics.elidedText(
                adjusted.text, adjusted.textElideMode, max(0, text_rect.width()-20))
        adjusted.widget.style().drawControl(QStyle.CE_ItemViewItem, adjusted, painter, adjusted.widget)
        if state:
            painter.save()
            from PySide6.QtGui import QPainter
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(STATES[state][0]))
            painter.drawEllipse(QRectF(option.rect.right()-15, option.rect.center().y()-4, 8, 8))
            painter.restore()

    def helpEvent(self, event, view, option, index):
        state = self.status(index)
        if state and event.pos().x() >= option.rect.right()-20:
            QToolTip.showText(event.globalPos(), STATES[state][1], view)
            return True
        return super().helpEvent(event, view, option, index)


def install_session_status(tree):
    tree.setItemDelegate(SessionStatusDelegate(tree))
