"""Docked, localized Markdown onboarding with in-app action links.

Each inline action link has the form [caption](toshy://onboarding/action-id).
The host window owns the allowlist of supported action IDs; links never open
external URLs or execute arbitrary commands.
"""
import time
from pathlib import Path

from PySide6.QtCore import Qt, QRectF, QTimer, Signal, QEvent
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QTextCursor, QBrush, QTextCharFormat
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from ui.dock_system.dock_panel import DockPanel
from ui.i18n import i18n, set_text, tr
from ui.icons import set_icon
from ui.themes.onboarding_theme import onboarding_colors
from ui.styles.binding import apply_style
from ui.themes.manager import theme_manager


# Pages are editable Markdown resources; the English directory defines their order.
PAGES_ROOT = Path(__file__).resolve().parents[1] / 'docs' / 'onboarding'
PAGE_IDS = tuple(path.stem for path in sorted((PAGES_ROOT / 'en').glob('[0-9][0-9].md')))


def load_page(page_id: str) -> str:
    """Load the active locale's page, falling back to the canonical English page."""
    language = i18n.current_lang
    for candidate in (PAGES_ROOT / language / f'{page_id}.md',
                      PAGES_ROOT / 'en' / f'{page_id}.md'):
        if candidate.is_file():
            try:
                return candidate.read_text(encoding='utf-8')
            except OSError:
                pass
    return f'# Missing onboarding page {page_id}'



class CompletionShade(QWidget):
    """Same three-second, 30-ms countdown surface as memory deletion."""
    def __init__(self, parent):
        super().__init__(parent)
        self.progress = 0.0
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.hide()

    def paintEvent(self, event):
        painter = QPainter(self)
        outline = QPainterPath()
        outline.addRoundedRect(QRectF(self.rect()), 6, 6)
        painter.setClipPath(outline)
        painter.fillRect(QRectF(0, 0, self.width() * self.progress, self.height()), QColor(0, 0, 0, 65))
        painter.end()


class OnboardingContent(QWidget):
    page_changed = Signal(int)
    completion_requested = Signal()
    action_requested = Signal(str)

    def __init__(self, initial_page=0, parent=None):
        super().__init__(parent)
        self.page_index = max(0, min(int(initial_page), max(0, len(PAGE_IDS) - 1)))
        self._closing_started = None
        self.setObjectName('OnboardingContent')

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.surface = QFrame(self)
        self.surface.setObjectName('OnboardingSurface')
        surface_layout = QVBoxLayout(self.surface)
        surface_layout.setContentsMargins(6, 5, 6, 6)
        surface_layout.setSpacing(5)
        layout.addWidget(self.surface)

        self.markdown = QTextBrowser(self.surface)
        self.markdown.setObjectName('OnboardingMarkdown')
        self.markdown.setFrameShape(QFrame.NoFrame)
        self.markdown.document().setDocumentMargin(0)
        self.markdown.setOpenLinks(False)
        self.markdown.setOpenExternalLinks(False)
        self.markdown.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse)
        self.markdown.anchorClicked.connect(self._open_action)
        apply_style(self.markdown,
            'QTextBrowser#OnboardingMarkdown {background:transparent; color:@text.primary@; '
            'border:0; padding:0; margin:0;}')
        surface_layout.addWidget(self.markdown, 1)

        self.navigation = QFrame(self.surface)
        self.navigation.setObjectName('OnboardingNavigation')
        self.navigation.setFixedHeight(36)
        self.navigation.installEventFilter(self)
        apply_style(self.navigation,
            'QFrame#OnboardingNavigation {background:@background.surface@; border:0; border-radius:6px;}')
        nav_layout = QHBoxLayout(self.navigation)
        nav_layout.setContentsMargins(4, 4, 4, 4)
        nav_layout.setSpacing(5)

        self.back = self._button('left', 'onboarding.previous', self._previous)
        nav_layout.addWidget(self.back)
        self.close_early = self._button('close', 'onboarding.finish', self._toggle_complete)
        nav_layout.addWidget(self.close_early)
        nav_layout.addStretch(1)
        self.page_counter = QLabel(self.navigation)
        self.page_counter.setObjectName('OnboardingPageCounter')
        self.page_counter.setAlignment(Qt.AlignCenter)
        apply_style(self.page_counter, 'QLabel#OnboardingPageCounter {color:@text.secondary@; background:transparent;}')
        nav_layout.addWidget(self.page_counter)
        nav_layout.addStretch(1)
        self.forward = self._button('right', 'onboarding.next', self._next_or_complete)
        nav_layout.addWidget(self.forward)
        surface_layout.addWidget(self.navigation)

        self.shade = CompletionShade(self.navigation)
        self.timer = QTimer(self)
        self.timer.setInterval(30)
        self.timer.timeout.connect(self._tick_completion)

        i18n.language_changed.connect(self._retranslate)
        theme_manager.theme_changed.connect(self._apply_theme)
        self._apply_theme()
        self._render_page()

    def _button(self, icon, tooltip_key, callback):
        button = QToolButton(self.navigation)
        button.setObjectName('OnboardingNavButton')
        apply_style(button, 'QToolButton#OnboardingNavButton {'
                    'border-radius:0; padding:0; margin:0;'
                    'background:@background.surface@; color:@text.primary@;'
                    'border:1px solid @border.subtle@;}'
                    'QToolButton#OnboardingNavButton:hover {'
                    'background:@control.hover@;}'
                    'QToolButton#OnboardingNavButton:disabled {'
                    'color:@text.disabled@;}')
        set_icon(button, icon, size=16, square=True)
        button.setFixedSize(28, 28)
        button.setCursor(Qt.PointingHandCursor)
        set_text(button, tooltip_key, setter='setToolTip')
        set_text(button, tooltip_key, setter='setAccessibleName')
        button.clicked.connect(callback)
        return button

    def _apply_theme(self, *_):
        colors = onboarding_colors()
        apply_style(self.surface,
            'QFrame#OnboardingSurface {background:@background.panel@; '
            f'border:2px solid {colors["outline"]}; border-radius:6px;}}')
        palette = self.markdown.palette()
        palette.setColor(QPalette.Link, QColor(colors['link']))
        palette.setColor(QPalette.LinkVisited, QColor(colors['link']))
        palette.setColor(QPalette.Text, theme_manager.color('text.primary'))
        self.markdown.setPalette(palette)
        self.markdown.document().setDefaultStyleSheet(
            f'a {{color:{colors["link"]};text-decoration:underline;}}'
            'body { margin:0; padding:0; }'
            'h1 {font-size:18px; margin:0 0 5px 0;}'
            'h2 {font-size:16px; margin:4px 0;}'
            'p {margin:0 0 4px 0;}'
            'ul, ol {margin:3px 0; padding-left:16px;}'
            'li {margin:0; padding:0;}'
        )
        self._render_markdown()

    def _render_markdown(self):
        position = self.markdown.verticalScrollBar().value()
        page_id = PAGE_IDS[self.page_index] if PAGE_IDS else '01'
        self.markdown.setMarkdown(load_page(page_id))
        # Qt Markdown can retain its own block margins despite the default CSS.
        # Normalize them without touching the list/anchor character formats.
        block = self.markdown.document().begin()
        while block.isValid():
            fmt = block.blockFormat()
            heading = fmt.headingLevel()
            fmt.setTopMargin(3 if heading else 0)
            fmt.setBottomMargin(5 if heading else 3)
            cursor = QTextCursor(block)
            cursor.setBlockFormat(fmt)
            block = block.next()
        # Qt Markdown gives links a built-in blue foreground in some versions,
        # overriding the palette and default document stylesheet. Apply the
        # theme-aware onboarding accent to each anchor fragment explicitly.
        link_format = QTextCharFormat()
        link_format.setForeground(QBrush(QColor(onboarding_colors()['link'])))
        link_format.setFontUnderline(True)
        anchor_ranges = []
        block = self.markdown.document().begin()
        while block.isValid():
            fragment = block.begin()
            while not fragment.atEnd():
                piece = fragment.fragment()
                if piece.isValid() and piece.charFormat().isAnchor():
                    anchor_ranges.append((piece.position(), piece.length()))
                fragment += 1
            block = block.next()
        for start, length in anchor_ranges:
            cursor = QTextCursor(self.markdown.document())
            cursor.setPosition(start)
            cursor.setPosition(start + length, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(link_format)
        self.markdown.verticalScrollBar().setValue(position)

    def _render_page(self):
        self._render_markdown()
        self.page_counter.setText(f'{self.page_index + 1} / {len(PAGE_IDS)}')
        last = self.page_index == len(PAGE_IDS) - 1
        self.back.setEnabled(self.page_index > 0)
        self.close_early.setVisible(not last)
        set_icon(self.close_early, 'close', size=16, square=True)
        set_text(self.close_early, 'onboarding.finish', setter='setToolTip')
        set_text(self.close_early, 'onboarding.finish', setter='setAccessibleName')
        set_icon(self.forward, 'close' if last else 'right', size=16, square=True)
        set_text(self.forward, 'onboarding.finish' if last else 'onboarding.next', setter='setToolTip')
        set_text(self.forward, 'onboarding.finish' if last else 'onboarding.next', setter='setAccessibleName')
        self.markdown.verticalScrollBar().setValue(0)

    def _retranslate(self, *_):
        if self._closing_started is not None:
            self.cancel_completion()
        self._render_page()

    def _open_action(self, url):
        if self._closing_started is not None:
            return
        # QTextBrowser emits QUrl for inline Markdown links. No web/file URLs.
        if url.scheme() != 'toshy' or url.host() != 'onboarding':
            return
        action = url.path().strip('/')
        if action and '/' not in action and action.replace('-', '').replace('_', '').isalnum():
            self.action_requested.emit(action)

    def _previous(self):
        self._change_page(self.page_index - 1)

    def _next_or_complete(self):
        if self.page_index == len(PAGE_IDS) - 1:
            self._toggle_complete()
        else:
            self._change_page(self.page_index + 1)

    def _change_page(self, target):
        if self._closing_started is not None or not 0 <= target < len(PAGE_IDS):
            return
        self.page_index = target
        self._render_page()
        self.page_changed.emit(target)

    def _toggle_complete(self):
        if self._closing_started is not None:
            self.cancel_completion()
            return
        self._closing_started = time.monotonic()
        active = self.forward if self.page_index == len(PAGE_IDS) - 1 else self.close_early
        set_icon(active, 'stop', size=16, square=True)
        set_text(active, 'onboarding.cancel', setter='setToolTip')
        set_text(active, 'onboarding.cancel', setter='setAccessibleName')
        self.back.setEnabled(False)
        if active is not self.forward:
            self.forward.setEnabled(False)
        self.shade.progress = 0.0
        self.shade.setGeometry(self.navigation.rect())
        self.shade.show()
        self.shade.raise_()
        active.raise_()  # Keep cancellation clickable above the countdown shade.
        self.timer.start()

    def cancel_completion(self):
        self._closing_started = None
        self.timer.stop()
        self.shade.hide()
        self.forward.setEnabled(True)
        self._render_page()

    def _tick_completion(self):
        if self._closing_started is None:
            return
        self.shade.progress = min(1.0, (time.monotonic() - self._closing_started) / 3.0)
        self.shade.update()
        if self.shade.progress >= 1.0:
            self.timer.stop()
            self._closing_started = None
            self.close_early.setEnabled(False)
            self.forward.setEnabled(False)
            self.completion_requested.emit()

    def eventFilter(self, watched, event):
        if (watched is self.navigation and event.type() == QEvent.Resize
                and hasattr(self, 'shade') and self.shade.isVisible()):
            self.shade.setGeometry(self.navigation.rect())
        return super().eventFilter(watched, event)


class OnboardingPanel(DockPanel):
    """Permanent dock tab until the explicit three-second Finish action."""
    def __init__(self, initial_page=0, parent=None):
        content = OnboardingContent(initial_page=initial_page)
        super().__init__(
            panel_id='onboarding_panel',
            title_key='docs.onboarding',
            content_widget=content,
            parent=parent,
        )
        self.page_changed = content.page_changed
        self.completion_requested = content.completion_requested
        self.action_requested = content.action_requested
