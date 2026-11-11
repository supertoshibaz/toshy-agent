"""External language packs with English fallback and explicit live text bindings."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from string import Formatter
from weakref import ref
from PySide6.QtCore import QObject, Signal, QSignalBlocker
from shiboken6 import isValid

log = logging.getLogger('toshy.ui.i18n')

class I18nManager(QObject):
    language_changed = Signal(str)

    def __init__(self, default_lang='en'):
        super().__init__()
        self.current_lang = default_lang
        self.locales_dir = Path(__file__).parent / 'locales'
        self._translations = {}
        self._bindings = {}
        self._warned = set()
        self._sources = {}
        self._load_all_locales()
        self.language_changed.connect(self._retranslate_bindings)

    def _load_all_locales(self):
        self._translations.clear()
        for file in sorted(self.locales_dir.glob('*.json')):
            try:
                value = json.loads(file.read_text(encoding='utf-8'))
                if not isinstance(value, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()):
                    raise ValueError('Language packs must map string keys to string values.')
                self._translations[file.stem] = value
            except (OSError, ValueError) as error:
                log.warning('Invalid language pack %s: %s', file.name, error)
        self._sources = {text: key for lang in ('ru', 'en') for key, text in self._translations.get(lang, {}).items() if text}

    def set_language(self, lang_code):
        if lang_code in self._translations and lang_code != self.current_lang:
            self.current_lang = lang_code
            self.language_changed.emit(lang_code)

    def get_text(self, key, default='', **parameters):
        if isinstance(key, LocalizedText):
            return key.resolve()
        english = self._translations.get('en', {})
        text = self._translations.get(self.current_lang, {}).get(key) or english.get(key)
        if not text:
            if key not in self._warned:
                self._warned.add(key)
                log.warning('Missing English UI translation: %s', key)
            text = default or '[' + key + ']'
        if parameters:
            try:
                return text.format(**{k: v.resolve() if isinstance(v, LocalizedText) else v for k, v in parameters.items()})
            except (ValueError, KeyError, IndexError) as error:
                log.warning('Invalid translation parameters for %s: %s', key, error)
                try:
                    return english.get(key, default or '[' + key + ']').format(**parameters)
                except (ValueError, KeyError, IndexError):
                    return '[' + key + ']'
        return text

    def source_key(self, text):
        return self._sources.get(str(text))

    def source(self, text):
        key = self.source_key(text)
        return self.get_text(key) if key else text

    def bind(self, target, key, *, setter='setText', index=None, **parameters):
        identity_key = id(target)
        record = self._bindings.get(identity_key)
        if record is None or record[0]() is not target:
            target_ref = ref(target, lambda _ref, key=identity_key: self._bindings.pop(key, None))
            record = (target_ref, {})
            self._bindings[identity_key] = record
        entries = record[1]
        identity = (setter, index)
        text = self.get_text(key, **parameters)
        if setter == 'setHorizontalHeaderText':
            item = target.horizontalHeaderItem(index)
            if item is not None:
                item.setText(text)
            method = None
        elif setter == 'setTreeHeaderText':
            target.headerItem().setText(index, text)
            method = None
        else:
            method = getattr(target, setter)
        blocker = QSignalBlocker(target) if isinstance(target, QObject) and setter in {'setItemText', 'setTabText'} else None
        try:
            if method is not None:
                method(text) if index is None else method(index, text)
        finally:
            if blocker is not None:
                blocker.unblock()
        entries[identity] = (key, parameters, text)
        return target

    def _retranslate_bindings(self):
        getters = {'setText': 'text', 'setToolTip': 'toolTip', 'setPlaceholderText': 'placeholderText',
                   'setWindowTitle': 'windowTitle', 'setTitle': 'title',
                   'setAccessibleName': 'accessibleName', 'setStatusTip': 'statusTip',
                   'setItemText': 'itemText', 'setTabText': 'tabText'}
        for identity_key, (target_ref, entries) in list(self._bindings.items()):
            target = target_ref()
            if target is None or not isValid(target):
                self._bindings.pop(identity_key, None)
                continue
            for identity, (key, parameters, previous) in list(entries.items()):
                setter, index = identity
                try:
                    if setter == 'setHorizontalHeaderText':
                        item = target.horizontalHeaderItem(index)
                        getter = lambda _index: item.text() if item is not None else None
                    elif setter == 'setTreeHeaderText':
                        getter = target.headerItem().text
                    else:
                        getter = getattr(target, getters.get(setter, ''), None)
                    if getter is not None:
                        current = getter() if index is None else getter(index)
                        # Components can replace a caption with user/model content.
                        if current != previous:
                            entries.pop(identity, None)
                            continue
                    self.bind(target, key, setter=setter, index=index, **parameters)
                except (RuntimeError, IndexError):
                    entries.pop(identity, None)

    def validate(self):
        english = self._translations.get('en', {})
        report = {}
        def fields(text):
            return {name for _, name, _, _ in Formatter().parse(text) if name is not None}
        for language, values in self._translations.items():
            invalid = []
            for key in english.keys() & values.keys():
                try:
                    if fields(english[key]) != fields(values[key]): invalid.append(key)
                except ValueError:
                    invalid.append(key)
            report[language] = {'missing': sorted(english.keys() - values.keys()),
                                'missing_english': sorted(values.keys() - english.keys()), 'invalid_parameters': invalid}
        return report

class LocalizedText(str):
    """A caption descriptor; composition keeps its live translation binding."""
    def __new__(cls, text, key=None, parameters=None, *, parts=None, default=''):
        value = super().__new__(cls, text)
        value.key, value.parameters, value.parts = key, parameters or {}, parts
        value.operation = None
        value.default = default
        return value

    def resolve(self):
        if self.operation is not None:
            base, name, args, kwargs = self.operation
            return getattr(base.resolve(), name)(*args, **kwargs)
        if self.parts is not None:
            return ''.join(part.resolve() if isinstance(part, LocalizedText) else str(part) for part in self.parts)
        return i18n.get_text(self.key, self.default, **self.parameters)

    def replace(self, *args, **kwargs):
        result = LocalizedText(str.replace(self, *args, **kwargs))
        result.operation = (self, 'replace', args, kwargs)
        return result

    def __add__(self, other):
        if not isinstance(other, str): return NotImplemented
        return LocalizedText(str.__add__(self, other), parts=(self, other))

    def __radd__(self, other):
        if not isinstance(other, str): return NotImplemented
        return LocalizedText(str.__add__(other, self), parts=(other, self))


i18n = I18nManager()

def tr(key, default='', **parameters):
    return LocalizedText(i18n.get_text(key, default, **parameters), key, parameters, default=default)

def localize(target, key, *, setter='setText', **parameters):
    return i18n.bind(target, key, setter=setter, **parameters)

def set_text(target, key, *, setter='setText', **parameters):
    i18n.bind(target, key, setter=setter, **parameters)

def bind_source(target, source, *, setter='setText'):
    if isinstance(source, LocalizedText):
        return i18n.bind(target, source, setter=setter)
    key = i18n.source_key(source)
    if key:
        i18n.bind(target, key, setter=setter)
    else:
        getattr(target, setter)(source)
    return target

def add_item(target, key, *args):
    index = target.count()
    result = target.addItem(tr(key), *args)
    if hasattr(target, 'setItemText'):
        i18n.bind(target, key, setter='setItemText', index=index)
    elif hasattr(target, 'item'):
        item = target.item(index)
        if item is not None:
            try:
                i18n.bind(item, key, index=0 if hasattr(target, 'columnCount') else None)
            except TypeError:
                pass
    return result

def add_row(target, key, field):
    target.addRow(tr(key), field)
    label = target.labelForField(field)
    if label is not None: i18n.bind(label, key)


def bind_caption(target, caption, *, setter='setText', index=None):
    """Bind marked application captions only. Plain user content is kept verbatim."""
    if isinstance(caption, LocalizedText):
        return i18n.bind(target, caption, setter=setter, index=index)
    method = getattr(target, setter)
    method(caption) if index is None else method(index, caption)
    return target


def add_caption_item(target, caption, *args, **kwargs):
    # addItem also belongs to graphics scenes and layouts. Only marked text
    # captions in item widgets need translation bookkeeping.
    if (not isinstance(caption, LocalizedText)
            or not callable(getattr(target, 'count', None))
            or not (callable(getattr(target, 'setItemText', None))
                    or callable(getattr(target, 'item', None)))):
        return target.addItem(caption, *args, **kwargs)
    index = target.count()
    result = target.addItem(caption, *args, **kwargs)
    if hasattr(target, 'setItemText'):
        bind_caption(target, caption, setter='setItemText', index=index)
    elif hasattr(target, 'item'):
        try:
            bind_caption(target.item(index), caption,
                         index=0 if hasattr(target, 'columnCount') else None)
        except TypeError:
            pass
    return result


def add_caption_items(target, captions):
    start = target.count()
    target.addItems(captions)
    for offset, caption in enumerate(captions):
        if isinstance(caption, LocalizedText):
            if hasattr(target, 'setItemText'):
                bind_caption(target, caption, setter='setItemText', index=start + offset)
            elif hasattr(target, 'item'):
                try:
                    bind_caption(target.item(start + offset), caption)
                except TypeError:
                    pass


def set_caption_headers(target, captions):
    target.setHeaderLabels(captions)
    for index, caption in enumerate(captions):
        if isinstance(caption, LocalizedText):
            bind_caption(target, caption, setter='setTreeHeaderText', index=index)


def add_caption_tab(target, widget, caption):
    index = target.addTab(widget, caption)
    bind_caption(target, caption, setter='setTabText', index=index)
    return index


def add_caption_action(target, method, *args, **kwargs):
    result = getattr(target, method)(*args, **kwargs)
    if result is not None:
        caption = next((value for value in args if isinstance(value, LocalizedText)), None)
        if caption is not None:
            bind_caption(result, caption, setter='setTitle' if method == 'addMenu' else 'setText')
    return result


def set_caption_table_headers(target, captions):
    target.setHorizontalHeaderLabels(captions)
    for index, caption in enumerate(captions):
        if isinstance(caption, LocalizedText):
            bind_caption(target, caption, setter='setHorizontalHeaderText', index=index)
