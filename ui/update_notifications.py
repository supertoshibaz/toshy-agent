"""One configurable background update check per launch and a hover-paused notice."""
from PySide6.QtCore import QEvent, QPoint, QTimer, Qt
from PySide6.QtWidgets import QFrame, QVBoxLayout, QLabel
from ui.i18n import tr
from ui.styles.binding import apply_style
from ui.settings import QSettings
from core.paths import APP_PATHS
from release_tools import client


def check_available_updates(cancel=None):
    """Independent failures must not hide a successful check of the other source."""
    result = {'application': None, 'plugins': []}
    try:
        release = client.check(cancel=cancel)
        if release.get('newer'):
            result['application'] = release
    except Exception:
        pass
    if cancel is not None and cancel.is_set():
        return result
    try:
        from core.plugins.store import PluginStore
        from core.plugins.updates import check_updates
        from ui.plugin_library import DEFAULT_PLUGIN_REPOSITORY
        store = PluginStore(APP_PATHS.root / 'plugins')
        catalog = str(QSettings('Toshy', 'Toshy').value('plugins/repository_url', DEFAULT_PLUGIN_REPOSITORY))
        if catalog and store.manifests(store.state()):
            result['plugins'] = [item for item in check_updates(store, catalog, cancel=cancel)
                                 if item['status'] == 'available']
    except Exception:
        pass
    return result


class UpdateNotice(QFrame):
    def __init__(self, window, result):
        super().__init__(window)
        self.host = window
        self.setObjectName('StartupUpdateNotice')
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedWidth(320)
        box = QVBoxLayout(self)
        box.setContentsMargins(12, 10, 12, 10)
        title = QLabel(tr('updates.available_title'), self)
        title.setAttribute(Qt.WA_TransparentForMouseEvents)
        box.addWidget(title)
        lines = []
        if result['application']:
            lines.append(tr('updates.available_app', version=result['application']['version']))
        if result['plugins']:
            lines.append(tr('updates.available_plugins', count=len(result['plugins'])))
        detail = QLabel('\n'.join(lines), self)
        detail.setWordWrap(True)
        detail.setAttribute(Qt.WA_TransparentForMouseEvents)
        box.addWidget(detail)
        apply_style(self, '''QFrame#StartupUpdateNotice {background:@background.panel@;
            color:@text.primary@;border:1px solid @border.subtle@;border-radius:8px;}
            QLabel {color:@text.primary@;background:transparent;border:0;}''')
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.close)
        self.remaining = 5000
        window.installEventFilter(self)
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()
        self.timer.start(self.remaining)

    def reposition(self):
        label = self.host.top_bar.title_label
        anchor = label.mapTo(self.host, QPoint(0, label.height() + 8))
        self.move(max(4, min(anchor.x(), self.host.width() - self.width() - 4)), anchor.y())

    def eventFilter(self, watched, event):
        if watched is self.host and event.type() in (QEvent.Resize, QEvent.Show):
            self.reposition()
            self.raise_()
        return super().eventFilter(watched, event)

    def enterEvent(self, event):
        if self.timer.isActive():
            self.remaining = max(1, self.timer.remainingTime())
            self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.timer.start(max(500, self.remaining))
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.close()
            self.host._open_updates()
            event.accept()
        else:
            super().mouseReleaseEvent(event)


def offer_automatic_check(window):
    if getattr(window, '_startup_updates_checked', False):
        return
    window._startup_updates_checked = True
    if not client.settings().get('automatic', True):
        return
    from threading import Event
    from ui.shutdown import on_shutdown
    cancel = Event()
    on_shutdown(cancel.set)

    def done(result):
        if result['application']:
            window.top_bar.update_action.setText(tr('updates.menu_available', version=result['application']['version']))
        if result['application'] or result['plugins']:
            window._update_notice = UpdateNotice(window, result)

    window.async_runner.submit(lambda: check_available_updates(cancel), done, lambda _: None)
