from __future__ import annotations
from ui.styles.binding import apply_style
from ui.i18n import bind_caption, tr, localize


from collections import Counter
from typing import Dict, Optional

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QFrame, QLabel, QGridLayout, QVBoxLayout, QWidget


class StartupTracker(QObject):
    """Flat startup barrier driven by actual UI work instead of a hard-coded phase list.

    Synchronous widget construction is held by one explicit token in ``ui.app``.
    Every AsyncRunner job started while startup is active automatically acquires its
    own token. Other asynchronous Qt models (currently QFileSystemModel) can acquire
    tokens directly. Readiness is emitted only after construction is sealed, no work
    is pending, and the event loop stays quiet briefly so zero-delay initializers can
    enqueue their own work first.
    """

    ready = Signal()
    phase_changed = Signal(str)

    def __init__(self, parent=None, settle_ms: int = 180):
        super().__init__(parent)
        self._pending: Dict[str, str] = {}
        self._counter = 0
        self._sealed = False
        self._finished = False
        self._generation = 0
        self._settle_ms = max(0, int(settle_ms))
        self._last_phase = None

    @property
    def pending(self):
        return set(self._pending.values())

    @property
    def is_ready(self) -> bool:
        return self._finished

    def begin(self, label: str) -> Optional[str]:
        if self._finished:
            return None
        self._counter += 1
        token = f"startup_{self._counter}"
        self._pending[token] = str(label or "ui-load")
        self._generation += 1
        if self._sealed:
            self._report_pending()
        return token

    def end(self, token: Optional[str]):
        if self._finished or not token:
            return
        if self._pending.pop(str(token), None) is None:
            return
        self._generation += 1
        if self._sealed:
            self._report_pending()
        self._schedule_ready_check()

    def seal(self):
        """Declare that the synchronous MainWindow construction pass has finished."""
        if self._finished:
            return
        self._sealed = True
        self._generation += 1
        self._report_pending()
        self._schedule_ready_check()

    def _report_pending(self):
        names = {'file': tr('ui.caption.d0ee1595fe03'), 'session': tr('ui.caption.e364b583b372'), 'project': tr('ui.caption.587175da7db8'),
                 'terminal': tr('ui.caption.3ad64e85281d'), 'task': tr('ui.caption.68a742cba73e'), 'module': tr('ui.caption.25f83bc28918'),
                 'startup': tr('ui.caption.f121faf421c8'), 'subagent': tr('ui.caption.171a7fbbc6c4'), 'mcp': 'MCP',
                 'paint': tr('ui.caption.655219af41d0'), 'board': tr('ui.caption.13632e78ed30'), 'chat': tr('ui.caption.c3f5aad5d18a')}
        groups = Counter()
        for label in self._pending.values():
            subject = next((text for key, text in names.items() if key in label.lower()), tr('ui.caption.03d139991c66'))
            groups[subject] += 1
        text = (tr('ui.caption.de0b5759283a') + str(len(self._pending)) + '\n'
                + ' · '.join(f'{name}: {count}' for name, count in sorted(groups.items()))
                if groups else tr('ui.caption.89bdbc54262d'))
        if text == self._last_phase:
            return
        self._last_phase = text
        self.phase_changed.emit(text)
        from ui.startup_process import report_startup_phase
        report_startup_phase(text)

    def _schedule_ready_check(self):
        if self._finished or not self._sealed or self._pending:
            return
        generation = self._generation
        QTimer.singleShot(
            self._settle_ms,
            lambda expected=generation: self._finish_if_still_quiet(expected),
        )

    def _finish_if_still_quiet(self, expected_generation: int):
        if self._finished or not self._sealed or self._pending:
            return
        if expected_generation != self._generation:
            self._schedule_ready_check()
            return
        self._finished = True
        self.ready.emit()


_ACTIVE_STARTUP_TRACKER: Optional[StartupTracker] = None


def set_active_startup_tracker(tracker: Optional[StartupTracker]):
    global _ACTIVE_STARTUP_TRACKER
    _ACTIVE_STARTUP_TRACKER = tracker


def begin_startup_activity(label: str) -> Optional[str]:
    tracker = _ACTIVE_STARTUP_TRACKER
    if tracker is None or tracker.is_ready:
        return None
    return tracker.begin(label)


def end_startup_activity(token: Optional[str]):
    tracker = _ACTIVE_STARTUP_TRACKER
    if tracker is None:
        return
    tracker.end(token)


from ui.widgets.orbital_loader import OrbitalLoader


class LoadingSpinner(OrbitalLoader):
    def __init__(self, parent=None):
        super().__init__(parent, size=52)
        self.setCursor(Qt.ArrowCursor)
        self.setFocusPolicy(Qt.NoFocus)


class StartupSplash(QWidget):
    def __init__(self):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setObjectName("ToshyStartupSplash")
        self.setFixedSize(430, 260)
        apply_style(self, """
            QWidget#ToshyStartupSplash {background:transparent;border:0;}
            QFrame#StartupSplashSurface {background:@background.panel@;border:1px solid @border.subtle@;border-radius:10px;}
            QLabel {color:@text.primary@;background:transparent;border:0;}
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.surface = QFrame(self)
        self.surface.setObjectName('StartupSplashSurface')
        self.surface.setAttribute(Qt.WA_StyledBackground, True)
        outer.addWidget(self.surface)
        # The spinner has a dedicated central row. Centre each text block in
        # the free space between its edge and the corresponding panel edge.
        layout = QGridLayout(self.surface)
        layout.setContentsMargins(28, 0, 28, 0)
        layout.setSpacing(0)
        layout.setRowStretch(0, 1)
        layout.setRowStretch(2, 1)

        title = self.title_label = localize(QLabel('', self), 'ui.top_bar.toshy_agent_studio_355a')
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet("font-size:22px; font-weight:700;")
        credit = self.credit_label = bind_caption(QLabel('', self), 'by supertoshibaz')
        credit.setAlignment(Qt.AlignCenter)
        apply_style(credit, 'color:@text.secondary@; font-size:11px;')
        credit.ensurePolished()
        title_box = QWidget(self.surface)
        title_layout = QVBoxLayout(title_box)
        title_layout.setContentsMargins(0, 0, 0, 0)
        title_layout.setSpacing(0)
        title_layout.addStretch(1)
        title_layout.addWidget(title)
        title_layout.addSpacing(4)
        title_layout.addWidget(credit)
        title_layout.addStretch(1)
        layout.addWidget(title_box, 0, 0)

        loading = self.status_label = localize(QLabel('', self), 'ui.startup.loading_interface_aea5')
        loading.setAlignment(Qt.AlignCenter)
        apply_style(loading, "color:@text.secondary@; font-size:11px;")
        loading.setWordWrap(True)
        loading.setMinimumHeight(34)
        layout.addWidget(loading, 2, 0)

        spinner = self.spinner = LoadingSpinner(self)
        layout.setRowMinimumHeight(1, spinner.height())
        layout.addWidget(spinner, 1, 0, Qt.AlignCenter)

    def show_centered(self):
        screen = self.screen()
        if screen is not None:
            geometry = screen.availableGeometry()
            self.move(geometry.center() - self.rect().center())
        self.show()
        self.raise_()

    @Slot(str)
    def set_phase(self, text):
        bind_caption(self.status_label, str(text))
