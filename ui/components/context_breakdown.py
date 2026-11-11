"""Expandable composition of the last measured context, without model polling."""
from PySide6.QtCore import Qt, QRectF, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import QWidget, QVBoxLayout, QGridLayout, QLabel, QToolButton, QSizePolicy
from ui.i18n import tr, set_text, bind_caption
from ui.icons import set_icon
from ui.styles.binding import apply_style, watch_paint
from ui.themes.manager import theme_manager

_expanded = False

COLOURS = {
    'messages': '#60a5fa', 'assistant': '#a78bfa', 'memory': '#fb923c',
    'tools': '#eab308', 'results': '#2dd4bf', 'system': '#94a3b8',
    'guidance': '#d991cb', 'summary': '#34d399', 'runtime': '#94a3b8',
    'unallocated': '#787e89', 'output': '#22c55e',
}


class CompositionBar(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(8)
        self.parts, self.capacity, self.colors = {}, 0, {}
        watch_paint(self)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        path = QPainterPath()
        path.addRoundedRect(rect, 4, 4)
        painter.setClipPath(path)
        painter.fillRect(rect, theme_manager.color('background.input'))
        if not self.capacity:
            return
        offset = 0.0
        for key, count in self.parts.items():
            width = self.width() * count / self.capacity
            painter.fillRect(QRectF(offset, 0, width, self.height()), QColor(self.colors.get(key, COLOURS.get(key, '#787e89'))))
            offset += width


class ContextBreakdown(QWidget):
    expanded_changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        self._row_widgets = {}
        self.toggle = QToolButton(self)
        self.toggle.setCheckable(True)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        set_text(self.toggle, 'ui.context_parts.title')
        set_text(self.toggle, 'ui.context_parts.title', setter='setToolTip')
        set_icon(self.toggle, 'down' if _expanded else 'right', size=14, icon_only=False)
        apply_style(self.toggle, 'QToolButton {color:@text.primary@;background:transparent;border:0;padding:3px 0;text-align:left;} QToolButton:hover {color:@accent.primary@;}')
        layout.addWidget(self.toggle)
        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(7)
        self.usage = QLabel(self.body)
        body.addWidget(self.usage)
        self.bar = CompositionBar(self.body)
        body.addWidget(self.bar)
        self.rows = QGridLayout()
        self.rows.setColumnStretch(1, 1)
        self.rows.setHorizontalSpacing(7)
        self.rows.setVerticalSpacing(5)
        body.addLayout(self.rows)
        self.note = QLabel(self.body)
        self.note.setWordWrap(True)
        apply_style(self.note, 'color:@text.secondary@;font-size:11px;')
        body.addWidget(self.note)
        layout.addWidget(self.body)
        self.toggle.setChecked(_expanded)
        self.body.setVisible(_expanded)
        self.toggle.toggled.connect(self._toggle)

    @Slot(bool)
    def _toggle(self, expanded):
        global _expanded
        _expanded = bool(expanded)
        set_icon(self.toggle, 'down' if expanded else 'right', size=14, icon_only=False)
        self.body.setVisible(expanded)
        self._sync_height()
        self.layout().activate()
        self.expanded_changed.emit()

    def _sync_height(self):
        if not hasattr(self, 'body'):
            return
        self.body.setVisible(self.toggle.isChecked())
        self.body.setMinimumHeight(0)
        self.body.layout().invalidate()
        self.layout().invalidate()
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def update_data(self, data):
        context = data.get('context') or {}
        measured = context.get('total')
        limit = data.get('limit')
        breakdown = data.get('breakdown') or {}
        parts = breakdown.get('parts') or {}
        complete = bool(parts) and breakdown.get('total') == measured
        if not complete:
            parts = {key: value for key, value in [('unallocated', context.get('input')),
                     ('output', context.get('output'))] if value}
        if measured is None:
            bind_caption(self.usage, tr('ui.context_parts.unknown'))
        elif limit:
            bind_caption(self.usage, tr('ui.context_parts.used', percent=100 * measured / limit))
        else:
            bind_caption(self.usage, tr('ui.context_parts.no_limit'))
        sections = breakdown.get('sections') or {}
        self.bar.colors = {key:value.get('color') for key,value in sections.items()}
        self.bar.parts, self.bar.capacity = parts, limit or measured or 0
        self.bar.update()
        entries = [(key, value) for key, value in parts.items() if value]
        if measured is not None and limit:
            entries.append(('free', max(0, limit - measured)))
        active = {key for key,_ in entries}
        for key in list(self._row_widgets):
            if key not in active:
                for widget in self._row_widgets.pop(key):
                    self.rows.removeWidget(widget);widget.hide();widget.deleteLater()
        for row,(key,value) in enumerate(entries):
            widgets=self._row_widgets.get(key)
            if widgets is None:
                widgets=tuple(QLabel(self.body) for _ in range(4))
                self._row_widgets[key]=widgets
                widgets[0].setFixedSize(9,9)
                widgets[1].setWordWrap(True)
                for widget in widgets[2:]:widget.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
            chip,label,count,percent=widgets
            section=sections.get(key,{})
            colour=section.get('color') or COLOURS.get(key,theme_manager.value('border.subtle'))
            chip.setStyleSheet(f'background:{colour};border:0;border-radius:2px;')
            if section:
                from ui.i18n import i18n
                labels=section.get('labels') or {}
                label.setText(labels.get(i18n.current_lang) or labels.get('en') or key)
            else:bind_caption(label,tr('ui.context_parts.'+key))
            approximate=complete and key not in {'output','free'}
            count.setText(('≈ ' if approximate else '')+f'{value:,}'.replace(',', '\u202f'))
            percent.setText(f'{100*value/limit:.1f}%' if limit else '—')
            for column,widget in enumerate(widgets):
                index=self.rows.indexOf(widget)
                if index<0 or self.rows.getItemPosition(index)[:2]!=(row,column):
                    self.rows.addWidget(widget,row,column)
                widget.show()
        bind_caption(self.note, tr('ui.context_parts.estimate' if complete else 'ui.context_parts.next_request'))
        self._sync_height()
