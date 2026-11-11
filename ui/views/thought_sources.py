"""Clickable, wrapping source chips at the bottom of a reasoning card."""
from ui.styles.binding import apply_style
from ui.i18n import bind_caption, tr

from urllib.parse import urlsplit
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QPushButton
from ui.views.inline_images import open_reference


def segment_sources(segments):
    """A site badge requires an actual read, including in older journals."""
    return [source for segment in segments for source in segment.get('ui_sources', [])
            if source.get('kind') != 'site' or
            (segment.get('name') in {'read_url', 'deep_search'} or source.get('tool') in {'read_url', 'deep_search'})]


def merge_sources(items):
    unique = {}
    domains = {urlsplit(item.get('url', '')).netloc.casefold():item for item in items
               if item.get('kind') == 'site' and item.get('recursive')}
    for item in items:
        if item.get('kind') not in {'site', 'file'} or not item.get('url'):
            continue
        if item['kind'] == 'site':
            item = domains.get(urlsplit(item['url']).netloc.casefold(), item)
        unique[(item['kind'], item['url'])] = item
    return list(unique.values())


class ThoughtSources(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sources = []
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 4, 0, 0)
        self.layout.setSpacing(6)

    def set_sources(self, sources):
        from ui.views.chat_view import FlowLayout
        merged = merge_sources(sources)
        if merged == self.sources:
            return
        self.sources = merged
        while self.layout.count():
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for kind, title in [('site', tr('ui.caption.54646f9a5e66')), ('file', tr('ui.caption.9088ca9c0de9'))]:
            entries = [s for s in self.sources if s['kind'] == kind]
            if not entries:
                continue
            label = bind_caption(QLabel('', self), title)
            apply_style(label, 'color: @text.secondary@; font-size: 11px; background: transparent;')
            self.layout.addWidget(label)
            row = QWidget(self)
            flow = FlowLayout(row, margin=0, h_spacing=6, v_spacing=6)
            for source in entries:
                text = source.get('label') or source['url']
                chip = bind_caption(QPushButton('', row), text if len(text) <= 52 else text[:49]+'…')
                chip.setMinimumHeight(26)
                chip.setCursor(Qt.PointingHandCursor)
                bind_caption(chip, source.get('path') or source['url'], setter='setToolTip')
                apply_style(chip, 'QPushButton {background: @background.panel@; color: @text.secondary@; border: 1px solid @border.subtle@; border-radius: 8px; padding: 4px 8px; font-size: 11px;} QPushButton:hover {border-color: @accent.primary@; color: white;}')
                chip.clicked.connect(lambda _checked=False, url=source['url']: open_reference(url, switch_view=True))
                flow.addWidget(chip)
            self.layout.addWidget(row)
