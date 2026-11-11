"""Compact chat input controls; menus reuse existing stateful selectors."""
from ui.i18n import add_caption_action, bind_caption, tr, bind_source, localize, set_text
from ui.icons import paint as paint_icon, bind_popup
from ui.styles.binding import watch_paint, apply_style


from math import ceil

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QMenu, QPushButton, QSizePolicy, QStyleOptionButton, QStyle


class ChatIconButton(QPushButton):
    def __init__(self, kind, title, parent=None):
        super().__init__(parent)
        self.kind, self.active = kind, False
        self.setObjectName("ChatInputAuxBtn")
        bind_source(self, title, setter='setAccessibleName')
        bind_source(self, title, setter='setToolTip')
        self.setProperty('iconOnly', True)
        watch_paint(self)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(32, 32)
        self.setStyleSheet("QPushButton {padding:0;min-width:30px;max-width:30px;min-height:30px;max-height:30px;}")

    def set_active(self, value):
        if self.active != bool(value):
            self.active = bool(value)
            self.setProperty('active', self.active)
            self.style().unpolish(self)
            self.style().polish(self)
            self.update()

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        try:
            paint_icon(painter, QRectF((self.width()-20)/2, (self.height()-20)/2, 20, 20),
                       self.kind, enabled=self.isEnabled(), active=self.active)
        finally:
            painter.end()


class ChatSendButton(QPushButton):
    """A readable shortcut followed by the send icon, including during a run."""
    def __init__(self, kind, title, parent=None):
        super().__init__(parent)
        self.kind = kind
        self._status = str(title)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(32)
        watch_paint(self)
        from ui.event_bus import event_bus
        event_bus.chat_send_shortcut_changed.connect(self._sync_caption)
        self._sync_caption()

    def setText(self, text):
        self._status = str(text)
        self._sync_caption()

    def _sync_caption(self):
        from ui.chat_hotkeys import send_label
        caption = send_label()
        super().setText(caption)
        self.setToolTip(self._status if self._status == caption else self._status + ' (' + caption + ')')
        self.setAccessibleName(self._status + ' (' + caption + ')')
        self.setFixedWidth(max(70, self.fontMetrics().horizontalAdvance(caption) + 50))
        self.update()

    def paintEvent(self, event):
        option = QStyleOptionButton()
        self.initStyleOption(option)
        option.text = ''
        painter = QPainter(self)
        self.style().drawControl(QStyle.CE_PushButton, option, painter, self)
        from ui.themes.manager import theme_manager
        painter.setPen(theme_manager.color('text.primary' if self.isEnabled() else 'text.disabled'))
        painter.drawText(self.rect().adjusted(10, 0, -34, 0), Qt.AlignVCenter | Qt.AlignLeft, self.text())
        paint_icon(painter, QRectF(self.width() - 28, 6, 20, 20), self.kind, enabled=self.isEnabled())
        painter.end()


class ModelTile(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.full_text = ""
        self.setTextFormat(Qt.PlainText)
        # A framed QLabel otherwise adds an implicit half-character indent
        # inside contentsRect(), which also has to fit the elided text.
        self.setIndent(0)
        self.setMargin(0)
        set_text(self, 'ui.chat_input_controls.selected_model_110a', setter='setAccessibleName')
        self.setFixedHeight(32)
        self.setMinimumWidth(32)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self.setContentsMargins(9, 0, 9, 0)
        apply_style(self, "QLabel {background:@background.panel@;border:1px solid @border.subtle@;border-radius:5px;color:@text.primary@;font-size:11px;}")

    def set_model(self, text, tooltip):
        self.full_text = text
        bind_caption(self, tooltip, setter='setToolTip')
        bind_caption(self, text, setter='setAccessibleDescription')
        self.update_text()
        self.updateGeometry()

    def sizeHint(self):
        margins = self.contentsMargins()
        metrics = QFontMetricsF(self.font())
        width = ceil(
            max(
                metrics.horizontalAdvance(self.full_text),
                metrics.boundingRect(self.full_text).right(),
            )
        )
        return QSize(max(32, width + margins.left() + margins.right() + 2), 32)

    def minimumSizeHint(self):
        return QSize(32, 32)

    def update_text(self):
        bind_caption(self, self.fontMetrics().elidedText(
                self.full_text, Qt.ElideMiddle, max(1, self.contentsRect().width() - 2)
            ))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.update_text()


def popup_above(menu, button):
    menu.ensurePolished()
    menu.adjustSize()
    anchor = button.mapToGlobal(QPoint(0, 0))
    available = button.screen().availableGeometry().adjusted(4, 4, -4, -4)
    menu.setMaximumHeight(max(1, anchor.y() - available.top()))
    width = min(menu.sizeHint().width(), available.width())
    menu.setMaximumWidth(available.width())
    x = max(available.left(), min(anchor.x(), available.right() - width + 1))
    menu.popup(
        QPoint(
            x,
            max(
                available.top(),
                anchor.y() - min(menu.sizeHint().height(), menu.maximumHeight()),
            ),
        )
    )


class ChatInputControls(QObject):
    def __init__(self, chat):
        super().__init__(chat)
        self.chat = chat
        self.info_button = ChatIconButton(
            "info", tr('ui.chat_input_controls.context_information_e898'), chat.input_card
        )
        self.tools_button = ChatIconButton(
            "wrench", tr('ui.chat_input_controls.request_settings_8c94'), chat.input_card
        )
        self.model_tile = ModelTile(chat.input_card)
        self.info_button.clicked.connect(self.open_info)
        self.tools_button.clicked.connect(self.open_tools)
        self.info_menu = bind_popup(QMenu(self.info_button), self.info_button)
        self.info_menu.setObjectName("ChatMessageContextMenu")
        self.system_action = localize(add_caption_action(self.info_menu, 'addAction', "Посмотреть системный промпт"), 'ui.chat_input_controls.view_system_prompt_6634')
        self.system_action.triggered.connect(chat.sys_prompt_btn.click)
        self.compressed_action = localize(add_caption_action(self.info_menu, 'addAction', "Посмотреть сжатый контекст"), 'ui.chat_input_controls.view_compacted_context_2ef5')
        self.compressed_action.triggered.connect(chat.compressed_context_btn.click)
        from ui.memory_manager import open_memory_manager
        self.memory_action = add_caption_action(self.info_menu, 'addAction', tr('ui.memory_manager.title'))
        self.memory_action.triggered.connect(lambda: open_memory_manager(chat))
        self.tools_menu = bind_popup(QMenu(self.tools_button), self.tools_button)
        self.tools_menu.setObjectName("ChatMessageContextMenu")
        self.raw_action = localize(add_caption_action(self.tools_menu, 'addAction', "Чистый запрос"), 'ui.chat_input_controls.clean_request_ad1d')
        self.raw_action.setCheckable(True)
        bind_caption(self.raw_action, chat.raw_request_btn.toolTip(), setter='setToolTip')
        self.raw_action.toggled.connect(chat.raw_request_btn.setChecked)
        self.full_web_action = localize(add_caption_action(self.tools_menu, 'addAction', 'Разрешить полное чтение веб-страниц в этом запросе'), 'ui.chat_input_controls.allow_full_web_page_reading_for_90df')
        self.full_web_action.setCheckable(True)
        set_text(self.full_web_action, 'ui.chat_input_controls.read_url_may_send_the_full_0aca', setter='setToolTip')
        self.full_web_action.toggled.connect(self.refresh_tools)
        self.pipeline_menu = localize(add_caption_action(self.tools_menu, 'addMenu', "Пайплайн"), 'ui.chat_input_controls.pipeline_c33e', setter='setTitle')
        self.pipeline_menu.setObjectName("ChatMessageContextMenu")
        from ui.widgets.skills_selector import SkillsSelector
        self.skills = SkillsSelector(chat, self.tools_menu)
        self.skills.changed.connect(self.refresh_tools)
        chat.raw_request_btn.toggled.connect(self.refresh_tools)
        self.pipeline_refresh = QTimer(self)
        self.pipeline_refresh.setSingleShot(True)
        self.pipeline_refresh.timeout.connect(self.refresh_pipeline)
        chat.pipeline_selector.display_changed.connect(
            lambda: self.pipeline_refresh.start(0)
        )
        chat.model_selector.display_changed.connect(self.model_tile.set_model)
        chat.model_selector.refresh()
        for button in (
            chat.sys_prompt_btn,
            chat.compressed_context_btn,
            chat.raw_request_btn,
            chat.pipeline_selector,
        ):
            button.hide()
        self.refresh_tools()
        self.refresh_pipeline()

    def refresh_tools(self, *_):
        chat = self.chat
        blocked = self.raw_action.blockSignals(True)
        try:
            self.raw_action.setChecked(chat.raw_request_btn.isChecked())
        finally:
            self.raw_action.blockSignals(blocked)
        self.raw_action.setEnabled(chat.raw_request_btn.isEnabled())
        self.full_web_action.setEnabled(not chat.raw_request_btn.isChecked())
        self.pipeline_menu.menuAction().setEnabled(chat.pipeline_selector.isEnabled())
        self.skills.menu.menuAction().setEnabled(chat.pipeline_selector.isEnabled())
        self.tools_button.set_active(
            chat.raw_request_btn.isChecked() or bool(chat.pipeline_selector.selected) or bool(self.skills.selected)
            or self.full_web_action.isChecked()
        )
        bind_caption(self.tools_button, tr('ui.caption.81ddead8e953')
            + (tr('ui.caption.35cfe273a48c') if chat.raw_request_btn.isChecked() else tr('ui.caption.2d2d65e8e86a'))
            + "\n"
            + chat.pipeline_selector.text()
            + (tr('ui.caption.763c585baf61') if self.full_web_action.isChecked() else ''), setter='setToolTip')

    def refresh_pipeline(self, *_):
        self.chat.pipeline_selector.populate_menu(self.pipeline_menu)
        bind_caption(self.pipeline_menu, tr('ui.caption.ea89d8d67379') + self.chat.pipeline_selector.text().removeprefix("Pipeline: "), setter='setTitle')
        self.refresh_tools()

    def set_enabled(self, value):
        self.info_button.setEnabled(value)
        self.tools_button.setEnabled(value)
        self.refresh_tools()

    def open_info(self):
        self.system_action.setEnabled(self.chat.sys_prompt_btn.isEnabled())
        self.compressed_action.setEnabled(self.chat.compressed_context_btn.isEnabled())
        popup_above(self.info_menu, self.info_button)

    def open_tools(self):
        self.refresh_tools()
        self.refresh_pipeline()
        popup_above(self.tools_menu, self.tools_button)
        self.chat.pipeline_selector.refresh()
