"""Local, outlined Tabler SVGs with bounded, theme-aware Qt caches."""
from functools import lru_cache
from pathlib import Path
from weakref import ref
from PySide6.QtCore import QObject, QRectF, QSize, Qt, Slot, qVersion
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QLabel
from shiboken6 import isValid
from ui.themes.manager import theme_manager

ROOT = Path(__file__).resolve().parent / 'assets' / 'tabler'
ALIASES = {
    'close': 'x', 'add': 'plus', 'remove': 'minus', 'save': 'device-floppy',
    'delete': 'trash', 'settings': 'settings', 'tools': 'tool', 'info': 'info-circle',
    'help': 'help-circle', 'search': 'search', 'folder': 'folder', 'file': 'file',
    'copy': 'copy', 'paste': 'clipboard', 'cut': 'cut', 'edit': 'pencil',
    'undo': 'arrow-back-up', 'redo': 'arrow-forward-up', 'run': 'player-play',
    'stop': 'player-stop', 'stop-thinking': 'message-forward', 'thinking': 'brain',
    'send': 'send', 'attach': 'paperclip', 'tag': 'tag', 'more': 'dots-vertical',
    'pin': 'pin', 'unpin': 'pinned-off', 'branch': 'git-branch', 'terminal': 'terminal-2',
    'memory': 'database', 'graph': 'topology-star-3', 'pipeline': 'route',
    'module': 'puzzle', 'plugin': 'plug', 'agent': 'robot', 'refresh': 'refresh',
    'warning': 'alert-triangle', 'error': 'alert-circle', 'success': 'check',
    'minimize': 'minus', 'maximize': 'square', 'restore': 'squares',
    'left': 'chevron-left', 'right': 'chevron-right', 'up': 'chevron-up', 'down': 'chevron-down',
    'expand': 'arrows-maximize', 'collapse': 'arrows-minimize', 'dock': 'layout-sidebar',
    'language': 'language', 'theme': 'palette', 'light': 'sun', 'dark': 'moon',
    'zoom-in': 'zoom-in', 'zoom-out': 'zoom-out', 'fit': 'focus-centered',
    'lock': 'lock', 'unlock': 'lock-open', 'eye': 'eye', 'eye-off': 'eye-off',
    'download': 'download', 'upload': 'upload', 'import': 'file-import', 'export': 'file-export',
    'link': 'link', 'unlink': 'unlink', 'check': 'check', 'cancel': 'x',
    'image': 'photo', 'audio': 'music', 'video': 'movie', 'code': 'code',
    'align-left': 'align-left', 'align-center': 'align-center', 'align-right': 'align-right',
    'bold': 'bold', 'italic': 'italic', 'underline': 'underline', 'strikethrough': 'strikethrough',
    'list': 'list', 'list-numbers': 'list-numbers', 'quote': 'blockquote',
    'table': 'table', 'calendar': 'calendar', 'clock': 'clock', 'history': 'history',
    'move': 'arrows-move', 'grip': 'grip-vertical', 'external': 'external-link',
    'clear': 'eraser', 'filter': 'filter', 'sort': 'arrows-sort', 'task': 'list-check',
    'power': 'power', 'sparkles': 'sparkles', 'book': 'book', 'document': 'file-text',
    'copy_markdown':'markdown', 'reply':'corner-up-left', 'forward':'corner-up-right',
    'retry':'refresh', 'select':'select', 'wrench':'tool', 'plus':'plus',
    'center':'focus-centered', 'axes':'axis-x', 'grid':'grid-dots', 'apply':'check',
    'background':'background', 'brush':'brush', 'eraser':'eraser', 'picker':'color-picker',
    'fill':'paint', 'text':'letter-t', 'select_rect':'select', 'select_lasso':'lasso',
    'transform':'transform', 'rectangle':'rectangle', 'ellipse':'oval', 'line':'line',
    'vector_text':'text-size', 'trash':'trash', 'eye_closed':'eye-off', 'adjustments':'adjustments-horizontal',
    'chevrons-down': 'chevrons-down', 'chevrons-up': 'chevrons-up',
}
SYMBOLS = {'✕':'close', '×':'close', '✖':'close', '+':'add', '−':'remove', '—':'minimize',
           '□':'maximize', '❐':'restore', '⋮':'more', '…':'more', '⚙':'settings', '⚙️':'settings',
           '🔧':'tools', '🔍':'search', '📁':'folder', '📂':'folder', '📄':'file', '💾':'save',
           '🗑':'delete', '🗑️':'delete', '📋':'paste', '↶':'undo', '↷':'redo',
           '▶':'right', '◀':'left', '▲':'up', '▼':'down', '▾':'down', '▸':'right',
           '🗗':'restore', '🗖':'maximize', '◔':'chart-pie','→':'arrow-right','⇄':'arrows-left-right','🌳':'sitemap',
           '⌫':'backspace','✦':'sparkles','≡':'list','🎨':'theme','⤒':'arrow-bar-to-up',
           '⤓':'arrow-bar-to-down','⌕':'search','⏸':'player-pause','!':'warning','▧':'image',
           '↩':'reply','⟲':'refresh','⧉':'copy','☰':'menu-2','⋮⋮':'grip','◇':'module', '⛶':'expand', '⟳':'refresh', '↻':'refresh', '↺':'undo', '✓':'check', '✔':'check',
           '?':'help', '🔗':'link', '📌':'pin', '✎':'edit', '⏹':'stop', '⏵':'run',
           '📎':'attach', '⚠':'warning', '⚠️':'warning', '↑':'up', '↓':'down'}


@lru_cache(maxsize=192)
def _renderer(name, color):
    filename = ALIASES.get(name, name)
    if not filename.replace('-', '').isalnum():
        raise ValueError('Invalid icon name.')
    path = ROOT / (filename + '.svg')
    if not path.is_file():
        path = ROOT / 'puzzle.svg'
    data = path.read_bytes().replace(b'currentColor', color.encode('ascii'))
    renderer = QSvgRenderer(data)
    if not renderer.isValid():
        raise ValueError('Invalid Tabler SVG: ' + filename)
    return renderer


class _IconEngine(QIconEngine):
    def __init__(self, name, tint=None):
        super().__init__()
        self.name, self.tint = name, tint

    def clone(self):
        return _IconEngine(self.name, self.tint)

    def paint(self, painter, rect, mode, state):
        role = ('icon.disabled' if mode == QIcon.Disabled else
                'icon.active' if state == QIcon.On or mode == QIcon.Selected else 'icon.normal')
        _renderer(self.name, self.tint if self.tint and mode != QIcon.Disabled else theme_manager.value(role)).render(painter, _square_rect(rect))

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):
        # Qt 6.8 changed this virtual's size from physical to logical pixels.
        legacy = tuple(int(v) for v in qVersion().split('.')[:2]) < (6, 8)
        logical_width = size.width() / scale if legacy else size.width()
        logical_height = size.height() / scale if legacy else size.height()
        image = QPixmap(max(1, round(logical_width * scale)), max(1, round(logical_height * scale)))
        image.setDevicePixelRatio(scale)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            self.paint(painter, QRectF(0, 0, logical_width, logical_height).toRect(), mode, state)
        finally:
            painter.end()
        return image


def _square_rect(rect):
    rect = QRectF(rect)
    side = min(rect.width(), rect.height())
    return QRectF(rect.center().x()-side/2, rect.center().y()-side/2, side, side)


def get(name, color=None):
    return QIcon(_IconEngine(name, color))


def paint(painter, rect, name, *, enabled=True, active=False):
    role = 'icon.disabled' if not enabled else 'icon.active' if active else 'icon.normal'
    _renderer(name, theme_manager.value(role)).render(painter, _square_rect(rect))


class _IconBinding(QObject):
    def __init__(self, button):
        super().__init__(button)
        self.button = ref(button)
        self.name = ''
        self.color = None
        theme_manager.theme_changed.connect(self.apply)

    @Slot()
    def apply(self):
        button = self.button()
        if button is not None and isValid(button):
            button.setIcon(get(self.name, self.color))
            button.update()


def set_icon(button, name, *, size=18, square=False, icon_only=True, color=None):
    # Labels display a pixmap, while buttons own a QIcon and iconSize. Dispatch
    # before installing a button binding so later theme changes stay type-safe.
    if isinstance(button, QLabel):
        return set_label_icon(button, name, size=size, color=color)
    binding = getattr(button, '_toshy_icon_binding', None)
    if binding is None:
        binding = button._toshy_icon_binding = _IconBinding(button)
    binding.name, binding.color = name, color
    from ui.i18n import bind_source
    caption = button.toolTip() or name.replace('-', ' ').capitalize()
    if not button.accessibleName():
        bind_source(button, caption, setter='setAccessibleName')
    if not button.toolTip():
        bind_source(button, caption, setter='setToolTip')
    if icon_only:
        button.setText('')
    button.setProperty('iconOnly', icon_only)
    button.setIconSize(QSize(size, size))
    if square:
        side = max(20, button.height() if button.minimumHeight() == button.maximumHeight() else 28)
        button.setFixedSize(side, side)
    binding.apply()
    return button


def symbol_button(button, text):
    """A compatibility adapter at explicit legacy symbol-button call sites."""
    name = SYMBOLS.get(text.strip())
    if name:
        set_icon(button, name, square=True)
    else:
        button.setText(text)
    return button


theme_manager.theme_changed.connect(lambda *_: _renderer.cache_clear())


class _LabelIconBinding(QObject):
    def __init__(self, label):
        super().__init__(label)
        self.label = ref(label)
        self.name, self.size, self.color = '', 18, None
        self.last_key = None
        theme_manager.theme_changed.connect(self.apply)

    @Slot()
    def apply(self):
        label = self.label()
        if label is not None and isValid(label):
            previous = label.pixmap()
            if self.last_key is not None and (previous is None or previous.cacheKey() != self.last_key):
                return
            ratio = label.devicePixelRatioF()
            pixmap = get(self.name, self.color).pixmap(QSize(self.size, self.size), ratio)
            label.setPixmap(pixmap)
            self.last_key = pixmap.cacheKey()


def set_label_icon(label, name, *, size=18, color=None):
    binding = getattr(label, '_toshy_label_icon_binding', None)
    if binding is None:
        binding = label._toshy_label_icon_binding = _LabelIconBinding(label)
    binding.name, binding.size, binding.color = name, size, color
    binding.last_key = None
    binding.apply()
    return label


def set_popup_active(button, active):
    if isValid(button) and bool(button.property('popupActive')) != bool(active):
        button.setProperty('popupActive', bool(active))
        button.style().unpolish(button)
        button.style().polish(button)
        button.update()


class _PopupBinding(QObject):
    def __init__(self, menu, button):
        super().__init__(menu)
        self.button = ref(button)
        menu.aboutToShow.connect(self.show)
        menu.aboutToHide.connect(self.hide)

    def show(self):
        button = self.button()
        if button is not None:
            set_popup_active(button, True)

    def hide(self):
        button = self.button()
        if button is not None:
            set_popup_active(button, False)


def bind_popup(menu, button):
    menu._toshy_popup_binding = _PopupBinding(menu, button)
    return menu
