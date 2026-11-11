"""Configurable composer shortcuts, stored in the agent's preferences folder."""
import json
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence
from core.paths import APP_PATHS
from core.private_files import atomic_private

_cache = {}


def normalize(sequence):
    sequence = QKeySequence(sequence)
    if sequence.count() != 1 or sequence.isEmpty():
        raise ValueError('Choose one key or a key combination.')
    combination = sequence[0]
    key = combination.key()
    if key in {Qt.Key_unknown, Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta} or not key:
        raise ValueError('The shortcut must include a key.')
    return sequence.toString(QKeySequence.PortableText)


def settings_path():
    return APP_PATHS.root / 'preferences/hotkeys.json'


def send_sequence():
    path = settings_path()
    if path not in _cache:
        try:
            value = json.loads(path.read_text(encoding='utf-8')).get('send_message', 'Ctrl+Return')
            _cache[path] = normalize(value)
        except (OSError, ValueError, TypeError, AttributeError):
            _cache[path] = 'Ctrl+Return'
    return QKeySequence(_cache[path])


def set_send_sequence(sequence):
    value = normalize(sequence)
    path = settings_path()
    atomic_private(path, (json.dumps({'send_message': value}, indent=2) + '\n').encode('utf-8'))
    _cache[path] = value
    from ui.event_bus import event_bus
    event_bus.chat_send_shortcut_changed.emit()


def send_label():
    return send_sequence().toString(QKeySequence.NativeText).replace("Return", "Enter")


def matches_send(event):
    combination = send_sequence()[0]
    key = Qt.Key_Return if event.key() == Qt.Key_Enter else event.key()
    expected = Qt.Key_Return if combination.key() == Qt.Key_Enter else combination.key()
    mask = Qt.ControlModifier | Qt.ShiftModifier | Qt.AltModifier | Qt.MetaModifier
    return key == expected and (event.modifiers() & mask) == (combination.keyboardModifiers() & mask)
