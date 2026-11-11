from ui.themes.manager import theme_manager
from ui.i18n import add_caption_action, bind_caption, tr, localize, set_text, i18n
from ui.styles.binding import apply_style, form_field
from ui.icons import set_popup_active
from ui.icons import set_icon, set_label_icon, symbol_button

import os
import re
import json
import math
import mimetypes
import tempfile
import time
from html import escape
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
from shiboken6 import isValid

from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton, QScrollArea, QLabel, QFrame, QToolButton, QCheckBox, QToolTip, QSizePolicy, QMenu, QLineEdit, QFileDialog, QLayout, QStyle, QApplication)
from ui.widgets.dialogs import InputDialog as QInputDialog
from ui.widgets.dialogs import MessageBox as QMessageBox
from PySide6.QtCore import Qt, Signal, QTimer, QTime, QPoint, QPointF, QLineF, QRect, QRectF, QSize, QEvent, QObject
from PySide6.QtGui import (
    QFont, QColor, QPainter, QPen, QBrush, QPalette,
    QKeyEvent, QKeySequence, QGuiApplication, QMouseEvent, QLinearGradient,
    QPixmap, QPolygonF, QPainterPath
)

from langchain_core.messages import HumanMessage, AIMessage
from ui.event_bus import event_bus
from ui.app_state import app_state
from ui.core_bridge import ChatStreamWorker, SessionApiClient, core_process
from ui.core_bridge.async_runner import AsyncRunner
from core.attachments import AttachmentError, inspect_attachment_path, resolve_attachment_path
from ui.views.chat_attachments import PendingAttachmentsPanel, MessageAttachmentsWidget
from ui.views.quote_input import InlineQuoteMixin
from ui.views.chat_prose import ChatProseView
from ui.styles.theme import EDITOR_TEXT_SIZE
from ui.task_status import build_active_task_list_prompt, build_task_status_prompt


# =========================================================================
# ТЕКСТ ПОДСКАЗКИ ДЛЯ ТИПОВ ПАМЯТИ
# =========================================================================
MEMORY_TOOLTIP_TEXT = (
    tr('ui.caption.d7bb8a4b9ebf')
)


# =========================================================================
# 1. КАРТОЧКА БЛОКА КОДА
# =========================================================================

from ui.views.code_block import CodeBlockWidget as ChatCodeBlockWidget


# =========================================================================
# 2. РЕНДЕРЕР MARKDOWN
# =========================================================================

class MarkdownMessageWidget(QWidget):
    def __init__(self, parent=None, *, text_color="#d4d4d8"):
        super().__init__(parent)
        from ui.themes.manager import theme_manager
        self._text_role = 'text.secondary' if text_color == '#a1a1aa' else 'text.primary'
        self.text_color = theme_manager.value(self._text_role)
        theme_manager.theme_changed.connect(self._theme_changed)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(0, 0, 0, 0)
        self.layout.setSpacing(2)
        self.layout.setAlignment(Qt.AlignTop)
        # Publish a complete layout after each render, never interpolate a
        # height smaller than the rich-text paragraphs need to paint.
        self.layout.setSizeConstraint(QLayout.SetNoConstraint)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(0)
        self._target_height = 0
        self._height_width = None
        self._height_measure_timer = QTimer(self)
        self._height_measure_timer.setSingleShot(True)
        self._height_measure_timer.timeout.connect(self._sync_height)
        self.raw_markdown = ""
        self._rendered_parts = []
        self._pending_markdown = None
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(33)
        self._render_timer.timeout.connect(self.flush_pending_markdown)

    def queue_markdown(self, text: str):
        """Coalesce streamed text only; the owning turn retains every delta.

        Do not restart an active timer: a continuous stream must keep painting.
        Event/segment boundaries flush explicitly before switching widgets.
        """
        if not self.raw_markdown and self._pending_markdown is None:
            self.set_markdown(text, streaming=True)
            return
        self._pending_markdown = text
        if not self._render_timer.isActive():
            self._render_timer.setInterval(min(150, 33 + len(text) // 2000))
            self._render_timer.start()

    def flush_pending_markdown(self):
        pending = self._pending_markdown
        if pending is not None:
            self.set_markdown(pending, streaming=True)

    def set_markdown(self, text: str, *, streaming=False):
        self._render_timer.stop()
        self._pending_markdown = None
        if text == self.raw_markdown:
            return
        self.raw_markdown = text

        from ui.views.chat_markdown import split_message
        parts = split_message(text, chunk_prose=False)
        for i, part in enumerate(parts):
            expected = ChatCodeBlockWidget if part.kind == 'code' else ChatProseView
            item = self.layout.itemAt(i).widget() if i < self.layout.count() else None
            created = not isinstance(item, expected)
            if created:
                # Keep the unchanged prefix, including a preview already opened
                # by the user. Never leave removed labels painted over new cards.
                while self.layout.count() > i:
                    removed = self.layout.takeAt(i).widget()
                    if removed:
                        removed.hide()
                        removed.deleteLater()
                if part.kind == 'code':
                    item = ChatCodeBlockWidget(part.text, language=part.language,
                                               parent=self, font_size=EDITOR_TEXT_SIZE)
                    item.presentation_changed.connect(lambda: self._height_measure_timer.start(0))
                else:
                    item = ChatProseView(self, self._open_reference)
                    item.geometry_changed.connect(lambda: self._height_measure_timer.start(0))
                self.layout.addWidget(item)
            if not created and i < len(self._rendered_parts) and part == self._rendered_parts[i]:
                continue
            if part.kind == 'code':
                item.set_code(part.text, language=part.language)
            else:
                item.set_markup(self._parse_markdown_to_html(part.text))
            if created:
                # Include the new fragment in this layout pass. Waiting for
                # Qt's deferred child-show event briefly shrinks the message
                # when a prose chunk becomes a code card or a new paragraph.
                item.show()
        while self.layout.count() > len(parts):
            removed = self.layout.takeAt(len(parts)).widget()
            if removed:
                removed.hide()
                removed.deleteLater()
        self._rendered_parts = parts
        self.layout.activate()
        self._sync_height()

    def _sync_height(self):
        width = max(1, self.contentsRect().width())
        self._height_width = width
        for index, part in enumerate(self._rendered_parts):
            item = self.layout.itemAt(index).widget()
            if isinstance(item, ChatProseView):
                item.setFixedHeight(item.heightForWidth(width))
        measured = self.layout.totalHeightForWidth(width)
        self._target_height = max(0, measured if measured >= 0 else self.layout.sizeHint().height())
        if self.height() != self._target_height or self.minimumHeight() != self._target_height:
            self.setFixedHeight(self._target_height)
        self.layout.activate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._height_width != self.contentsRect().width():
            self._sync_height()

    def event(self, event):
        result = super().event(event)
        if event.type() == QEvent.LayoutRequest and hasattr(self, '_height_measure_timer'):
            # Styled code cards can finish polishing after the parent's first
            # measurement. Fold/preview changes also arrive through this path.
            self._height_measure_timer.start(0)
        return result

    def _theme_changed(self, *_):
        from ui.themes.manager import theme_manager
        self.text_color = theme_manager.value(self._text_role)
        for index, part in enumerate(self._rendered_parts):
            if part.kind != 'code' and index < self.layout.count():
                label = self.layout.itemAt(index).widget()
                if isinstance(label, ChatProseView):
                    label.set_markup(self._parse_markdown_to_html(part.text))
        self._sync_height()

    def _open_reference(self, reference):
        from ui.views.inline_images import open_reference
        open_reference(reference, switch_view=True)

    def _format_inline(self, text: str) -> str:
        """Инлайн-форматирование текста (жирный, курсив, зачеркивание, код, ссылки)."""
        # Message text is Markdown, not trusted Qt rich text. In particular,
        # unfenced SVG/XML in reasoning must stay visible instead of becoming
        # unsupported HTML tags with only their line breaks left on screen.
        html = escape(text, quote=True)
        html = re.sub(r"\*\*\*(.*?)\*\*\*", r"<b><i>\1</i></b>", html)
        html = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", html)
        html = re.sub(r"\*(.*?)\*", r"<i>\1</i>", html)
        html = re.sub(r"~~(.*?)~~", r"<s>\1</s>", html)
        selected = bool(self.property('messageSelected'))
        parent = self.parentWidget()
        while parent is not None and not selected:
            selected = bool(parent.property('messageSelected'))
            parent = parent.parentWidget()
        background = theme_manager.value('control.hover' if selected else 'background.surface')
        foreground = theme_manager.value('text.primary' if selected else 'accent.primary')
        html = re.sub(r"`([^`]+)`", lambda m: "<code style='background-color:" + background
                      + ";color:" + foreground + ";font-family:monospace;'>" + m[1] + "</code>", html)
        html = re.sub(r"\[([^\[\]\n]*)\]\(([^()\[\]\n]*)\)", r"<a href='\2' style='color:#38bdf8; text-decoration:underline;'>\1</a>", html)
        return html

    def _split_table_row(self, row_str: str) -> List[str]:
        s = row_str.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    def _is_table_separator(self, row_str: str) -> bool:
        cells = self._split_table_row(row_str)
        if not cells:
            return False
        return all(re.match(r"^:?-{2,}:?$", c) for c in cells)

    def _render_table_html(self, headers: List[str], aligns: List[str], body_rows: List[List[str]]) -> str:
        from ui.themes.manager import token
        html = [f"<table style='border-collapse: collapse; width: 100%; margin: 6px 0; font-size:12px; border: 1px solid {token('border.subtle')};'>"]
        html.append(f"<thead><tr style='background-color: {token('background.surface')};'>")
        for idx, h_cell in enumerate(headers):
            align = aligns[idx] if idx < len(aligns) else "left"
            formatted = self._format_inline(h_cell)
            html.append(f"<th align='{align}' style='border: 1px solid {token('border.subtle')}; padding: 6px 10px; color: {token('text.primary')}; text-align: {align}; font-weight: bold;'>{formatted}</th>")
        html.append("</tr></thead><tbody>")

        for r_idx, row_cells in enumerate(body_rows):
            bg = token('background.panel' if r_idx % 2 == 0 else 'background.window')
            html.append(f"<tr style='background-color: {bg};'>")
            for idx in range(len(headers)):
                cell_text = row_cells[idx] if idx < len(row_cells) else ""
                align = aligns[idx] if idx < len(aligns) else "left"
                formatted = self._format_inline(cell_text)
                html.append(f"<td align='{align}' style='border: 1px solid {token('border.subtle')}; padding: 5px 10px; color: {token('text.primary')}; text-align: {align};'>{formatted}</td>")
            html.append("</tr>")
        html.append("</tbody></table>")
        return "".join(html)

    def _parse_markdown_to_html(self, md: str) -> str:
        from ui.themes.manager import token
        # 1. Нормализация переносов строк и удаление мусорных пробелов на пустых строках
        md = md.replace("\r\n", "\n").replace("\r", "\n")
        md = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", md)
        # Срезаем стартовые/конечные переносы, чтобы карточки не начинались с пустых строк
        md = md.strip("\n")
        if not md.strip():
            return ""

        lines = md.split("\n")
        html_parts = []
        i = 0
        n = len(lines)

        while i < n:
            line = lines[i]
            stripped = line.strip()

            # Пустая строка (абзацный отступ между обычным текстом)
            if not stripped:
                html_parts.append("<p style='margin:0;-qt-paragraph-type:empty;'><br/></p>")
                i += 1
                continue

            # 2. Таблицы GFM
            if "|" in line and i + 1 < n and self._is_table_separator(lines[i + 1]):
                headers = self._split_table_row(line)
                sep_cells = self._split_table_row(lines[i + 1])
                aligns = []
                for c in sep_cells:
                    if c.startswith(":") and c.endswith(":"):
                        aligns.append("center")
                    elif c.endswith(":"):
                        aligns.append("right")
                    else:
                        aligns.append("left")

                i += 2
                body_rows = []
                while i < n and "|" in lines[i] and not lines[i].strip().startswith("```"):
                    body_rows.append(self._split_table_row(lines[i]))
                    i += 1

                html_parts.append(self._render_table_html(headers, aligns, body_rows))
                continue

            if stripped in ("*", "-"):
                i += 1
                continue

            # 3. Горизонтальный разделитель
            if re.fullmatch(r"(?:-\s*){3,}|(?:_\s*){3,}|(?:\*\s*){3,}", stripped):
                html_parts.append("<hr color='#3f3f46'/>")
                i += 1
                continue

            # 4. Заголовки (компактные отступы без лишних <br>)
            h_match = re.match(r"^(#{1,6})\s+(.*)$", line)
            if h_match:
                level = len(h_match.group(1))
                h_text = self._format_inline(h_match.group(2))
                size = EDITOR_TEXT_SIZE + (2 if level == 1 else 1 if level == 2 else 0)
                heading_color = self.text_color
                margin_top = "6px" if html_parts else "0px"
                html_parts.append(f"<p style='font-size:{size}px; font-weight:bold; color:{heading_color}; margin:{margin_top} 0 2px 0; padding:0; line-height:1.2;'>{h_text}</p>")
                i += 1
                continue

            # 5. Цитаты (Синяя линия слева, текст сдвинут)
            q_match = re.match(r"^\s*>\s*(.*)$", line)
            if q_match:
                q_text = self._format_inline(q_match.group(1))
                html_parts.append(f"<p style='border-left:3px solid #38bdf8; padding-left:12px; margin:3px 0 3px 8px; color:#a1a1aa; font-style:italic;'>{q_text}</p>")
                i += 1
                continue

            # 6. Маркированные списки (Сноска слева, точка перед текстом)
            list_match = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
            if list_match:
                content = self._format_inline(list_match.group(2))
                margin = 16 + len(list_match.group(1).expandtabs(4)) * 4
                html_parts.append(f"<p style='margin:1px 0 1px {margin}px; padding:0; line-height:1.15;'><span style='color:#818cf8;'>• </span>{content}</p>")
                i += 1
                continue

            # 7. Нумерованные списки
            num_match = re.match(r"^(\s*)(\d+[\.\)])\s+(.*)$", line)
            if num_match:
                prefix = num_match.group(2)
                content = self._format_inline(num_match.group(3))
                margin = 16 + len(num_match.group(1).expandtabs(4)) * 4
                html_parts.append(f"<p style='margin:1px 0 1px {margin}px; padding:0; line-height:1.15;'><span style='color:#818cf8; font-weight:bold;'>{prefix} </span>{content}</p>")
                i += 1
                continue

            # 8. Обычный текст
            formatted_line = self._format_inline(line)
            html_parts.append("<p style='margin:0; padding:0;'>" + formatted_line + "</p>")
            i += 1

        result_html = "".join(html_parts)
        return f"<div style='color:{self.text_color}; font-size:{EDITOR_TEXT_SIZE}px;'>{result_html}</div>"


# =========================================================================
# 3. КАРТОЧКА ВЫЗОВА ИНСТРУМЕНТА
# =========================================================================

class ToolExecutionCard(QFrame):
    def __init__(self, tool_name: str, args: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.setObjectName("ToolExecutionCard")
        self.tool_name = tool_name
        self.tool_args = dict(args or {})
        self.result_output = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)

        self.status_icon = set_label_icon(QLabel('', self), 'settings')
        self.title_label = bind_caption(QLabel('', self), f"<b>tool:</b> {tool_name}")
        apply_style(self.title_label, "color: #38bdf8; font-family: 'Fira Code', monospace; font-size:11px; background: transparent;")

        self.status_label = localize(QLabel('', self), 'ui.chat_view.running_c886')
        self.status_label.setStyleSheet("color: #eab308; font-size:10px; font-style: italic; background: transparent;")

        header.addWidget(self.status_icon)
        header.addWidget(self.title_label)
        header.addWidget(self.status_label)
        header.addStretch(1)
        layout.addLayout(header)

        args_str = json.dumps(args, ensure_ascii=False, indent=2)
        self.args_label = localize(QLabel('', self), 'ui.chat_view.arguments_v0_4388', v0=args_str)
        apply_style(self.args_label, "color: @text.secondary@; font-family: 'Fira Code', monospace; font-size:10px; background: transparent;")
        self.args_label.setWordWrap(True)
        self.args_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.args_label)

        self.result_label = QLabel("", self)
        apply_style(self.result_label, "color: @text.disabled@; font-family: 'Fira Code', monospace; font-size:10px; background: transparent;")
        self.result_label.setWordWrap(True)
        self.result_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.result_label.hide()
        layout.addWidget(self.result_label)

    def set_result(self, output: str, status: str = 'completed'):
        self.result_output = str(output or "")
        uncertain = status in {'unknown', 'incomplete'}
        failed = status == 'failed'
        stopped = status == 'stopped'
        bind_caption(self.status_icon, '×' if failed else '■' if stopped else '?' if uncertain else '✓')
        bind_caption(self.status_label, tr('ui.caption.1c31246b0b64') if status == 'incomplete' else tr('ui.caption.c394012b6f11') if uncertain else tr('ui.caption.44d4090ae9e4') if failed else tr('ui.caption.df14a196ee9a') if stopped else tr('ui.caption.57778825e18b'))
        apply_style(self.status_label, f"color: {'#ef4444' if failed else '@text.secondary@' if stopped else '#eab308' if uncertain else '#22c55e'}; font-size:10px; background: transparent;")

        clean_preview = output.strip()
        if len(clean_preview) > 250:
            clean_preview = clean_preview[:250] + tr('ui.caption.182d58ca2266')

        set_text(self.result_label, 'ui.chat_view.result_v0_cdda', v0=clean_preview)
        self.result_label.show()


# =========================================================================
# 4. СТАРТОВЫЙ ЭКРАН ВЫБОРА ПАМЯТИ
# =========================================================================

class ChatWelcomeWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.selected_mode = "text"
        self.store_both_enabled = True
        self.include_thoughts_enabled = True

        outer = QVBoxLayout(self)
        outer.setAlignment(Qt.AlignCenter)
        self.panel = QFrame(self)
        self.panel.setObjectName('ChatWelcomePanel')
        self.panel.setMinimumWidth(480)
        self.panel.setMaximumWidth(660)
        layout = QVBoxLayout(self.panel)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)
        outer.addWidget(self.panel, 0, Qt.AlignCenter)
        self.motto = QLabel(self.panel)
        self.motto.setWordWrap(True)
        self.motto.setAlignment(Qt.AlignCenter)
        apply_style(self.motto, 'color:@text.primary@;font-size:20px;font-weight:600;background:transparent;')
        self._motto_key = None
        layout.addWidget(self.motto)
        prompt_label = localize(QLabel('', self.panel), 'ui.chat.welcome.prompt')
        prompt_label.setAlignment(Qt.AlignCenter)
        prompt_label.setWordWrap(True)
        apply_style(prompt_label, 'font-size:15px;color:@text.primary@;font-weight:600;')
        layout.addWidget(prompt_label)

        self.settings_toggle = localize(QToolButton(self.panel), 'ui.chat.welcome.settings')
        self.settings_toggle.setObjectName('ChatWelcomeSettingsToggle')
        self.settings_toggle.setCheckable(True)
        self.settings_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.settings_toggle.setCursor(Qt.PointingHandCursor)
        set_text(self.settings_toggle, 'ui.chat.welcome.settings', setter='setToolTip')
        set_icon(self.settings_toggle, 'right', size=16, icon_only=False)
        layout.addWidget(self.settings_toggle, 0, Qt.AlignCenter)
        self.settings_body = QWidget(self.panel)
        self.settings_body.setObjectName('ChatWelcomeSettings')
        settings_layout = QVBoxLayout(self.settings_body)
        settings_layout.setContentsMargins(0, 6, 0, 0)
        settings_layout.setSpacing(14)
        layout.addWidget(self.settings_body)
        self.settings_toggle.toggled.connect(self._toggle_settings)
        self.settings_body.hide()

        header_row = QHBoxLayout()
        header_row.setAlignment(Qt.AlignCenter)
        header_row.setSpacing(6)

        title = localize(QLabel('', self), 'ui.chat_view.choose_session_memory_type_acb0')
        apply_style(title, "font-size:13px; color: @text.secondary@; font-weight: bold;")

        help_badge = set_label_icon(QLabel('', self), 'help')
        help_badge.setObjectName("MemoryHelpBadge")
        bind_caption(help_badge, MEMORY_TOOLTIP_TEXT, setter='setToolTip')
        help_badge.setCursor(Qt.WhatsThisCursor)

        header_row.addWidget(title)
        header_row.addWidget(help_badge)
        settings_layout.addLayout(header_row)

        btn_row = QHBoxLayout()
        btn_row.setAlignment(Qt.AlignCenter)
        btn_row.setSpacing(12)

        self.btn_pages = localize(QPushButton('', self), 'ui.chat_view.pages_text_memory_ba20')
        self.btn_pages.setObjectName("MemoryModeBtn")
        self.btn_pages.setCheckable(True)
        self.btn_pages.setChecked(True)
        self.btn_pages.clicked.connect(lambda: self._set_mode("text"))

        self.btn_vectors = localize(QPushButton('', self), 'ui.chat_view.vectors_embeddings_c3e9')
        self.btn_vectors.setObjectName("MemoryModeBtn")
        self.btn_vectors.setCheckable(True)
        self.btn_vectors.setChecked(False)
        self.btn_vectors.clicked.connect(lambda: self._set_mode("vector"))

        btn_row.addWidget(self.btn_pages)
        btn_row.addWidget(self.btn_vectors)
        settings_layout.addLayout(btn_row)

        self.store_both_checkbox = localize(QCheckBox(
            '',
            self,
        ), 'ui.chat_view.store_temporary_memory_uploads_as_both_699c')
        self.store_both_checkbox.setChecked(True)
        self.store_both_checkbox.toggled.connect(
            lambda checked: setattr(self, "store_both_enabled", bool(checked))
        )
        settings_layout.addWidget(self.store_both_checkbox, 0, Qt.AlignLeft)

        self.include_thoughts_checkbox = localize(QCheckBox('', self), 'ui.chat_view.include_model_thoughts_in_context_8f9a')
        self.include_thoughts_checkbox.setChecked(True)
        set_text(self.include_thoughts_checkbox, 'ui.chat_view.you_can_change_this_in_projects_ec55', setter='setToolTip')
        self.include_thoughts_checkbox.toggled.connect(
            lambda checked: setattr(self, "include_thoughts_enabled", bool(checked))
        )
        settings_layout.addWidget(self.include_thoughts_checkbox, 0, Qt.AlignLeft)
        self.include_replied_thoughts_checkbox = localize(QCheckBox('', self), 'ui.chat_view.include_thoughts_from_the_model_message_c577')
        set_text(self.include_replied_thoughts_checkbox, 'ui.chat_view.when_general_thought_inclusion_is_disabled_2a44', setter='setToolTip')
        self.include_replied_thoughts_checkbox.setEnabled(not self.include_thoughts_checkbox.isChecked())
        self.include_thoughts_checkbox.toggled.connect(lambda checked: self.include_replied_thoughts_checkbox.setEnabled(not checked))
        settings_layout.addWidget(self.include_replied_thoughts_checkbox, 0, Qt.AlignLeft)
        prompt_title = localize(QLabel('', self.settings_body), 'ui.module_view.system_prompt_2074')
        settings_layout.addWidget(prompt_title)
        self.system_prompt_edit = form_field(QTextEdit(self.settings_body))
        self.system_prompt_edit.setAcceptRichText(False)
        self.system_prompt_edit.setFixedHeight(100)
        set_text(self.system_prompt_edit, 'ui.projects_view.additional_system_prompt_2800', setter='setPlaceholderText')
        settings_layout.addWidget(self.system_prompt_edit)

        from core.memory_preference import last_memory_mode
        self._set_mode(last_memory_mode(), remember=False)

    def _toggle_settings(self, expanded):
        self.settings_body.setVisible(expanded)
        set_icon(self.settings_toggle, 'down' if expanded else 'right', size=16, icon_only=False)
        self.panel.updateGeometry()

    def showEvent(self, event):
        if not event.spontaneous():
            self.settings_toggle.setChecked(False)
            from random import choice
            choices = [f'ui.chat.welcome.motto.{i}' for i in range(1, 7)]
            self._motto_key = choice([key for key in choices if key != self._motto_key])
            set_text(self.motto, self._motto_key)
        super().showEvent(event)

    def _set_mode(self, mode: str, *, remember=True):
        if remember:
            from core.memory_preference import remember_memory_mode
            remember_memory_mode(mode)
        self.selected_mode = mode
        self.btn_pages.setChecked(mode == "text")
        self.btn_vectors.setChecked(mode == "vector")

    def reset_defaults(self):
        from core.memory_preference import last_memory_mode
        self._set_mode(last_memory_mode(), remember=False)
        self.store_both_enabled = True
        self.store_both_checkbox.setChecked(True)
        self.include_thoughts_enabled = True
        self.include_thoughts_checkbox.setChecked(True)
        self.include_replied_thoughts_checkbox.setChecked(False)
        self.system_prompt_edit.clear()


# =========================================================================
# 5. ОТДЕЛЬНОЕ СООБЩЕНИЕ РАЗМЫШЛЕНИЙ МОДЕЛИ
# =========================================================================

class ThoughtMessageTurn(QFrame):
    delete_requested = Signal(object)
    create_branch_requested = Signal(object)
    content_edited = Signal()

    def __init__(self, parent=None, *, defer_body=False):
        super().__init__(parent)
        self.setObjectName("ThoughtMessageCard")
        self._defer_body = defer_body
        from ui.views.chat_references import turn_metadata
        turn_metadata(self)
        self.is_expanded = True
        self.is_editing = False
        self.setMouseTracking(True)
        self.start_time = time.time()
        self.elapsed_seconds = 0.0
        self.tools_count = 0
        self.tool_cards: Dict[str, ToolExecutionCard] = {}
        self.current_thought_view: Optional[MarkdownMessageWidget] = None
        self.current_thought_raw: str = ""
        self.total_thought_text: str = ""
        self.transcript_segments: List[Dict[str, Any]] = []
        self.sources_panel = None

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(8, 4, 8, 0)
        self.main_layout.setSpacing(4)

        header_row = QHBoxLayout()
        header_row.setSpacing(0)

        from ui.widgets.orbital_loader import OrbitalLoader
        self.chevron = OrbitalLoader(self, size=20)
        self.chevron.setObjectName('ThoughtActivityButton')
        set_text(self.chevron, 'ui.chat_view.model_reasoning_a157', setter='setAccessibleName')
        self.chevron.clicked.connect(lambda: self.toggle_collapse(preserve_view=True))
        self.disclosure_btn = QToolButton(self)
        self.disclosure_btn.setObjectName('ThoughtToggleBtn')
        self.disclosure_btn.setFixedSize(12, 20)
        self.disclosure_btn.setCursor(Qt.PointingHandCursor)
        set_text(self.disclosure_btn, 'ui.chat_view.model_reasoning_a157', setter='setAccessibleName')
        set_icon(self.disclosure_btn, 'chevron-down', size=12)
        self.disclosure_btn.clicked.connect(lambda: self.toggle_collapse(preserve_view=True))
        self.header_btn = localize(QPushButton('', self), 'ui.chat_view.model_reasoning_a157')
        self.header_btn.setObjectName("ThoughtHeaderBtn")
        self.header_btn.setCursor(Qt.PointingHandCursor)
        self.header_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.header_btn.clicked.connect(lambda: self.toggle_collapse(preserve_view=True))
        self.meta_badge = localize(QLabel('', self), 'ui.chat_view.thinking_0_0_s_5357')
        self.meta_badge.setObjectName("ThoughtMetaBadge")
        apply_style(self.meta_badge, 'padding:0;')
        header_row.addWidget(self.chevron)
        header_row.addSpacing(6)
        header_row.addWidget(self.disclosure_btn)
        header_row.addSpacing(6)
        header_row.addWidget(self.header_btn)
        # The logo and elapsed text sit equally far from the title. The
        # left-hand interval also contains the separate disclosure arrow.
        header_row.addSpacing(24)
        header_row.addWidget(self.meta_badge)
        header_row.addStretch(1)
        self.main_layout.addLayout(header_row)

        self.body_container = QWidget(self)
        self.body_container.setObjectName("ThoughtBodyContainer")
        self.body_layout = QVBoxLayout(self.body_container)
        self.body_layout.setContentsMargins(0, 6, 0, 0)
        self.body_layout.setSpacing(5)
        self.main_layout.addWidget(self.body_container)

        self.edit_box = QWidget(self)
        self.edit_box.setObjectName("ChatInlineEditorBox")
        edit_layout = QVBoxLayout(self.edit_box)
        edit_layout.setContentsMargins(0, 4, 0, 0)
        edit_layout.setSpacing(6)

        self.editor_text = form_field(QTextEdit(self.edit_box))
        self.editor_text.setObjectName("ChatInlineEditor")
        edit_layout.addWidget(self.editor_text)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.save_btn = localize(QPushButton('', self.edit_box), 'ui.settings.save_b4d3')
        self.save_btn.setObjectName("ChatInlineSaveBtn")
        self.save_btn.clicked.connect(self._save_edit)
        self.cancel_btn = localize(QPushButton('', self.edit_box), 'ui.close_panel.cancel_8fbe')
        self.cancel_btn.setObjectName("ChatInlineCancelBtn")
        self.cancel_btn.clicked.connect(self._cancel_edit)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)
        edit_layout.addLayout(btn_row)

        self.edit_box.hide()
        self.main_layout.addWidget(self.edit_box)
        # Thoughts have no independent action row; the answer owns the block.

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

    def enterEvent(self, event):
        from ui.views.chat_model_blocks import owner
        bar = getattr(owner(self), 'actions_bar', None)
        if bar is not None:
            bar.refresh_visibility()
        super().enterEvent(event)

    def leaveEvent(self, event):
        from ui.views.chat_model_blocks import owner
        bar = getattr(owner(self), 'actions_bar', None)
        if bar is not None:
            bar.refresh_visibility()
        super().leaveEvent(event)

    def _on_tick(self):
        self.elapsed_seconds = time.time() - self.start_time
        tools_str = tr('ui.caption.939e30e5e5d3', v0=self.tools_count) if self.tools_count > 0 else ""
        set_text(self.meta_badge, 'ui.chat_view.thinking_v0_1f_s_v1_60fd', v0=self.elapsed_seconds, v1=tools_str)

    def _reset_body_widgets(self):
        # История файлов относится ко всему запуску и переживает пересборку текста.
        chip = getattr(self, 'file_history_btn', None)
        if chip is not None and not isValid(chip):
            del self.file_history_btn
            chip = None
        for index in range(self.body_layout.count() - 1, -1, -1):
            widget = self.body_layout.itemAt(index).widget()
            if widget is chip and chip is not None:
                continue
            self.body_layout.takeAt(index)
            if widget is not None and isValid(widget):
                widget.hide()
                widget.deleteLater()
        self.sources_panel = None
        self.current_thought_view = None
        self.current_thought_raw = ''
        self.tool_cards = {}

    def _add_body_widget(self, widget):
        position = self.body_layout.indexOf(self.sources_panel) if self.sources_panel is not None and isValid(self.sources_panel) else -1
        chip = getattr(self, 'file_history_btn', None)
        if position < 0 and chip is not None and isValid(chip):
            position = self.body_layout.indexOf(chip)
        self.body_layout.insertWidget(position if position >= 0 else self.body_layout.count(), widget)

    def add_tool_sources(self, tool_id, sources):
        if not sources:
            return
        for segment in reversed(self.transcript_segments):
            if segment.get('type') == 'tool' and segment.get('id') == tool_id:
                segment['ui_sources'] = list(sources)
                break
        self._refresh_sources()

    def _refresh_sources(self):
        if self._defer_body:
            return
        from ui.views.thought_sources import segment_sources
        sources = segment_sources(self.transcript_segments)
        if not sources:
            if self.sources_panel is not None and isValid(self.sources_panel):
                self.sources_panel.set_sources([])
                self.sources_panel.hide()
            return
        if self.sources_panel is None or not isValid(self.sources_panel):
            from ui.views.thought_sources import ThoughtSources
            self.sources_panel = ThoughtSources(self.body_container)
            chip=getattr(self,'file_history_btn',None)
            position=self.body_layout.indexOf(chip) if chip is not None and isValid(chip) else -1
            if position>=0:self.body_layout.insertWidget(position,self.sources_panel)
            else:self.body_layout.addWidget(self.sources_panel)
        self.sources_panel.set_sources(sources)
        self.sources_panel.show()

    def append_thought(self, chunk: str):
        self.total_thought_text += chunk
        if self.transcript_segments and self.transcript_segments[-1].get("type") == "reasoning":
            self.transcript_segments[-1]["text"] = self.transcript_segments[-1].get("text", "") + chunk
        else:
            self.transcript_segments.append({"type": "reasoning", "text": chunk})
        if self._defer_body:
            return
        if not self.current_thought_view:
            self.current_thought_view = MarkdownMessageWidget(self.body_container, text_color="#a1a1aa")
            self.current_thought_raw = ""
            self._add_body_widget(self.current_thought_view)

        self.current_thought_raw += chunk
        self.current_thought_view.queue_markdown(self.current_thought_raw)

    def add_tool_start(self, tool_name: str, args: dict, tool_id: str = ""):
        if self.current_thought_view is not None:
            self.current_thought_view.flush_pending_markdown()
        self.tools_count += 1
        self.current_thought_view = None
        self.current_thought_raw = ""
        self.transcript_segments.append({
            "type": "tool",
            "id": str(tool_id or ""),
            "name": str(tool_name or "tool"),
            "args": dict(args or {}),
            "output": "",
            "status": "running",
        })

        if self._defer_body:
            return

        card = ToolExecutionCard(tool_name, args, self.body_container)
        key = tool_id or tool_name
        self.tool_cards[key] = card
        self.tool_cards[tool_name] = card
        self._add_body_widget(card)

    def add_tool_result(self, tool_id: str, tool_name: str, output: str, status: str = 'completed'):
        if self.current_thought_view is not None:
            self.current_thought_view.flush_pending_markdown()
        for segment in reversed(self.transcript_segments):
            if segment.get("type") != "tool":
                continue
            if tool_id and str(segment.get("id") or "") == str(tool_id):
                segment["output"] = str(output or "")
                segment['status'] = status
                break
            if not tool_id and str(segment.get("name") or "") == str(tool_name) and not segment.get("output"):
                segment["output"] = str(output or "")
                segment['status'] = status
                break

        if self._defer_body:
            return
        if tool_id and tool_id in self.tool_cards:
            self.tool_cards[tool_id].set_result(output, status)
        elif tool_name and tool_name in self.tool_cards:
            self.tool_cards[tool_name].set_result(output, status)
        else:
            for card in self.tool_cards.values():
                if card.status_label.text() == tr('ui.chat_view.running_c886'):
                    card.set_result(output, status)
                    break

        self.current_thought_view = None
        self.current_thought_raw = ""

    def pause_tools(self):
        for segment in list(self.transcript_segments):
            if segment.get('type') == 'tool' and segment.get('status', 'running') == 'running' and not segment.get('output'):
                self.add_tool_result(str(segment.get('id') or ''), str(segment.get('name') or ''),
                                     tr('ui.caption.ff86bfd4f5b9'),
                                     status='unknown')

    def export_segments(self) -> List[Dict[str, Any]]:
        segments = [dict(segment) for segment in self.transcript_segments]
        if segments:
            segments[0]["phase_seconds"] = round(max(0.0, float(self.elapsed_seconds)), 3)
        return segments

    def finish_thinking(self, elapsed_seconds: Optional[float] = None):
        if self.current_thought_view is not None:
            self.current_thought_view.flush_pending_markdown()
        was_active = False
        try:
            if hasattr(self, "_timer") and self._timer is not None:
                was_active = self._timer.isActive()
                self._timer.stop()
        except RuntimeError:
            pass

        if elapsed_seconds is not None:
            self.elapsed_seconds = max(0.0, float(elapsed_seconds))
        elif was_active:
            # If the timer was already stopped by the first final-content chunk,
            # keep that value. A later request-finished event must not add answer
            # generation time to the recorded thinking duration.
            self.elapsed_seconds = time.time() - self.start_time

        tools_str = tr('ui.caption.939e30e5e5d3', v0=self.tools_count) if self.tools_count > 0 else ""
        set_text(self.meta_badge, 'ui.chat_view.v0_1f_s_v1_3152', v0=self.elapsed_seconds, v1=tools_str)
        self.sync_activity()

    def sync_activity(self):
        set_icon(self.disclosure_btn, 'chevron-down' if self.is_expanded else 'chevron-right', size=12)
        state = (getattr(self, '_run_state', None) or {}).get('status')
        if not state:
            active = self._timer.isActive() or any(result.get('status') in {'queued', 'running'}
                                                   for result in getattr(self, 'results', []))
            state = 'running' if active else 'completed'
        self.chevron.set_state('running' if state in {'running', 'queued', 'finalizing'}
                               else 'completed' if state == 'completed' else 'interrupted')

    def auto_collapse(self, elapsed_seconds: Optional[float] = None):
        self.finish_thinking(elapsed_seconds=elapsed_seconds)
        if self.is_expanded:
            self.toggle_collapse()

    def resume_thinking(self):
        """Возвращает карточку в активное состояние после отзыва provisional-ответа."""
        try:
            if hasattr(self, "_timer") and self._timer is not None and not self._timer.isActive():
                self._timer.start()
        except RuntimeError:
            return
        if not self.is_expanded:
            self.toggle_collapse()
        self._on_tick()
        self.sync_activity()

    def toggle_collapse(self, *, preserve_view=False):
        if preserve_view:
            from ui.views.chat_references import controller_for
            controller = controller_for(self)
            if controller is not None:
                controller.chat._hold_thought_position(self)
        self.is_expanded = not self.is_expanded
        if self.is_expanded:
            self._materialize_thought_body()
        self.body_container.setVisible(self.is_expanded and not self.is_editing)
        self.edit_box.setVisible(self.is_expanded and self.is_editing)
        self.chevron.setAccessibleDescription(self.header_btn.text())
        set_icon(self.disclosure_btn, 'chevron-down' if self.is_expanded else 'chevron-right', size=12)
        self.main_layout.invalidate()
        self.updateGeometry()

    def _materialize_thought_body(self):
        if not self._defer_body:
            return
        self._defer_body = False
        segments = self.transcript_segments
        total_text = self.total_thought_text
        tools_count = self.tools_count
        self.transcript_segments = []
        self.total_thought_text = ""
        self.tools_count = 0
        try:
            for segment in segments:
                if segment.get("type") == "reasoning":
                    if self.total_thought_text:
                        self.append_thought('\n\n')
                    self.append_thought(str(segment.get("text") or ""))
                    if self.transcript_segments:
                        self.transcript_segments[-1]['ui_sources'] = segment.get('ui_sources') or []
                elif segment.get("type") == "tool":
                    identity = str(segment.get("id") or "")
                    name = str(segment.get("name") or "tool")
                    self.add_tool_start(name, dict(segment.get("args") or {}), identity)
                    if segment.get("output"):
                        self.add_tool_result(identity, name, str(segment["output"]), segment.get('status', 'completed'))
                    self.add_tool_sources(identity, segment.get('ui_sources') or [])
        except Exception:
            self._reset_body_widgets()
            self._defer_body = True
            raise
        finally:
            # Ошибка отрисовки не должна стирать данные для сохранения и контекста.
            self.tools_count = tools_count
            self.transcript_segments = segments
            self.total_thought_text = total_text
        if self.current_thought_view is not None:
            self.current_thought_view.flush_pending_markdown()
        self._refresh_sources()

    def _toggle_edit_mode(self):
        if not self.is_editing:
            self.editor_text.setPlainText(self.total_thought_text)
            self.body_container.hide()
            self.edit_box.show()
            self.is_editing = True
            self.is_expanded = True
            set_icon(self.disclosure_btn, 'chevron-down', size=12)
        else:
            self._cancel_edit()

    def _save_edit(self):
        self._defer_body = False
        self.total_thought_text = self.editor_text.toPlainText().strip()
        from ui.views.thought_sources import segment_sources
        ui_sources = segment_sources(self.transcript_segments)
        self.transcript_segments = (
            [{"type": "reasoning", "text": self.total_thought_text, "ui_sources": ui_sources}]
            if self.total_thought_text else []
        )
        self._reset_body_widgets()
        self.current_thought_view = MarkdownMessageWidget(self.body_container, text_color="#a1a1aa")
        self.current_thought_view.set_markdown(self.total_thought_text)
        self._add_body_widget(self.current_thought_view)
        self._refresh_sources()

        self.edit_box.hide()
        self.body_container.setVisible(self.is_expanded)
        self.is_editing = False
        self.content_edited.emit()

    def _cancel_edit(self):
        self._materialize_thought_body()
        self.edit_box.hide()
        self.body_container.setVisible(self.is_expanded)
        self.is_editing = False

# =========================================================================
# 6. КАРТОЧКИ СООБЩЕНИЙ
# =========================================================================

class CanvasContextPreviewWidget(QWidget):
    """Very light, read-only projection of a canvas fragment.

    One QWidget paints the entire graph into a cached pixmap.  No QGraphicsScene,
    card widgets, Markdown renderers or per-node child widgets are created, so
    long chats can contain many canvas fragments without multiplying the cost
    of the full Board implementation.
    """

    def __init__(self, context: Dict[str, Any], parent=None):
        super().__init__(parent)
        self.context = dict(context or {})
        self._cache: Optional[QPixmap] = None
        self.setObjectName("CanvasContextPreview")
        self.setMinimumWidth(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(self._desired_height_for_width(520))

    def _source_extent(self) -> Tuple[float, float]:
        nodes = list(self.context.get("nodes") or [])
        if not nodes:
            return 1.0, 1.0
        min_x = min(float(n.get("x", 0)) for n in nodes)
        min_y = min(float(n.get("y", 0)) for n in nodes)
        max_x = max(float(n.get("x", 0)) + max(1.0, float(n.get("width", 1))) for n in nodes)
        max_y = max(float(n.get("y", 0)) + max(1.0, float(n.get("height", 1))) for n in nodes)
        return max(1.0, max_x - min_x), max(1.0, max_y - min_y)

    def _desired_height_for_width(self, width: int) -> int:
        # Preserve the source board aspect ratio instead of enlarging cards
        # independently.  This makes the preview a true uniform projection of
        # the selected board fragment while still letting square/tall fragments
        # use more vertical space in Chat.
        src_w, src_h = self._source_extent()
        content_w = max(1.0, float(width) - 20.0)
        graph_h = content_w * (src_h / src_w)
        return int(max(210.0, min(620.0, 41.0 + graph_h)))

    def sizeHint(self):
        w = 520
        return QSize(w, self._desired_height_for_width(w))

    def resizeEvent(self, event):
        self._cache = None
        desired = self._desired_height_for_width(event.size().width())
        if abs(self.height() - desired) > 1:
            self.setFixedHeight(desired)
        super().resizeEvent(event)

    def _render_cache(self):
        if self.width() <= 0 or self.height() <= 0:
            return
        pix = QPixmap(self.size())
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        p.setRenderHint(QPainter.Antialiasing)

        outer = QRectF(0.5, 0.5, self.width() - 1.0, self.height() - 1.0)
        p.setPen(QPen(QColor("#34343a"), 1.0))
        p.setBrush(QBrush(QColor("#121214")))
        p.drawRoundedRect(outer, 7, 7)

        nodes = list(self.context.get("nodes") or [])
        edges = list(self.context.get("edges") or [])
        explicit = set(self.context.get("explicitNodes") or [])
        include_path = bool(self.context.get("includePath", False))
        path = str(self.context.get("canvasPath") or tr('nav.board')) if include_path else tr('ui.caption.877e74b662d3')

        p.setPen(QColor("#a1a1aa"))
        header_font = QFont(self.font())
        header_font.setPointSizeF(max(8.0, header_font.pointSizeF() - 1.0))
        p.setFont(header_font)
        header = tr('ui.caption.a138a94b497b', v0=path, v1=len(nodes), v2=len(edges))
        p.drawText(QRectF(10, 6, self.width() - 20, 20), Qt.AlignLeft | Qt.AlignVCenter, header)

        content = QRectF(10, 31, max(1, self.width() - 20), max(1, self.height() - 41))
        if not nodes:
            p.setPen(QColor("#71717a"))
            p.drawText(content, Qt.AlignCenter, tr('ui.caption.00c1742b1b3b'))
            p.end()
            self._cache = pix
            return

        min_x = min(float(n.get("x", 0)) for n in nodes)
        min_y = min(float(n.get("y", 0)) for n in nodes)
        max_x = max(float(n.get("x", 0)) + max(1.0, float(n.get("width", 1))) for n in nodes)
        max_y = max(float(n.get("y", 0)) + max(1.0, float(n.get("height", 1))) for n in nodes)
        src_w = max(1.0, max_x - min_x)
        src_h = max(1.0, max_y - min_y)
        scale = min(content.width() / src_w, content.height() / src_h)
        draw_w = src_w * scale
        draw_h = src_h * scale
        off_x = content.left() + (content.width() - draw_w) / 2.0
        off_y = content.top() + (content.height() - draw_h) / 2.0

        # Uniformly project every geometry component.  x/y/width/height all use
        # the same scale, so relative card positions and spacing are exactly the
        # same as on the source board.  Do not independently enlarge/clamp cards:
        # that was the source of overlapping previews.
        projected: Dict[str, QRectF] = {}
        for node in nodes:
            x = off_x + (float(node.get("x", 0)) - min_x) * scale
            y = off_y + (float(node.get("y", 0)) - min_y) * scale
            w = max(1.0, float(node.get("width", 1)) * scale)
            h = max(1.0, float(node.get("height", 1)) * scale)
            projected[str(node.get("id") or "")] = QRectF(x, y, w, h)

        def port_point(rect: QRectF, side: str) -> QPointF:
            points = {
                "top_left": QPointF(rect.left(), rect.top()),
                "top": QPointF(rect.center().x(), rect.top()),
                "top_right": QPointF(rect.right(), rect.top()),
                "right": QPointF(rect.right(), rect.center().y()),
                "bottom_right": QPointF(rect.right(), rect.bottom()),
                "bottom": QPointF(rect.center().x(), rect.bottom()),
                "bottom_left": QPointF(rect.left(), rect.bottom()),
                "left": QPointF(rect.left(), rect.center().y()),
            }
            return points.get(side, rect.center())

        def side_vector(side: str, magnitude: float) -> QPointF:
            d = magnitude * 0.7071
            vectors = {
                "top": QPointF(0, -magnitude),
                "bottom": QPointF(0, magnitude),
                "left": QPointF(-magnitude, 0),
                "right": QPointF(magnitude, 0),
                "top_left": QPointF(-d, -d),
                "top_right": QPointF(d, -d),
                "bottom_left": QPointF(-d, d),
                "bottom_right": QPointF(d, d),
            }
            return vectors.get(side, QPointF(0, 0))

        # Reproduce the board's directed cubic connections using the same port
        # sides and control-point rule as CanvasArrowItem.
        edge_color = QColor("#85858f")
        for edge in edges:
            a = projected.get(str(edge.get("fromNode") or ""))
            b = projected.get(str(edge.get("toNode") or ""))
            if a is None or b is None:
                continue

            from_side = str(edge.get("fromSide") or "bottom")
            to_side = str(edge.get("toSide") or "top")
            p1 = port_point(a, from_side)
            p2 = port_point(b, to_side)

            # CanvasArrowItem computes this offset in scene units.  Applying the
            # preview scale to it preserves the exact curve shape after projection.
            src_a_x = (p1.x() - off_x) / max(scale, 1e-9) + min_x
            src_a_y = (p1.y() - off_y) / max(scale, 1e-9) + min_y
            src_b_x = (p2.x() - off_x) / max(scale, 1e-9) + min_x
            src_b_y = (p2.y() - off_y) / max(scale, 1e-9) + min_y
            dist_src = math.hypot(src_b_x - src_a_x, src_b_y - src_a_y)
            offset = max(30.0, min(dist_src * 0.4, 120.0)) * scale
            ctrl1 = p1 + side_vector(from_side, offset)
            ctrl2 = p2 + side_vector(to_side, offset)

            path = QPainterPath(p1)
            path.cubicTo(ctrl1, ctrl2, p2)
            p.setPen(QPen(edge_color, max(1.0, 1.8 * scale)))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

            tangent = p2 - ctrl2
            angle = math.atan2(tangent.y(), tangent.x())
            arrow_size = max(4.0, 11.0 * scale)
            sin_a = math.sin(angle)
            cos_a = math.cos(angle)
            left = QPointF(
                p2.x() - arrow_size * cos_a + (arrow_size / 2.0) * sin_a,
                p2.y() - arrow_size * sin_a - (arrow_size / 2.0) * cos_a,
            )
            right = QPointF(
                p2.x() - arrow_size * cos_a - (arrow_size / 2.0) * sin_a,
                p2.y() - arrow_size * sin_a + (arrow_size / 2.0) * cos_a,
            )
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(edge_color))
            p.drawPolygon(QPolygonF([p2, left, right]))

        show_text = len(nodes) <= 80
        node_font = QFont(self.font())
        node_font.setPointSizeF(max(7.0, node_font.pointSizeF() - 2.0))
        p.setFont(node_font)
        fm = p.fontMetrics()

        for node in nodes:
            node_id = str(node.get("id") or "")
            rect = projected.get(node_id)
            if rect is None:
                continue
            border = QColor("#818cf8") if node_id in explicit else QColor(node.get("color") or "#52525b")
            p.setPen(QPen(border, 1.4 if node_id in explicit else 1.0))
            p.setBrush(QBrush(QColor("#19191c")))
            p.drawRoundedRect(rect, min(5.0, rect.height() / 4.0), min(5.0, rect.height() / 4.0))

            if show_text and rect.width() >= 28 and rect.height() >= 14:
                raw = str(node.get("text") or "").strip().splitlines()
                label = raw[0] if raw else ""
                if label:
                    label = fm.elidedText(label, Qt.ElideRight, max(8, int(rect.width() - 8)))
                    p.setPen(QColor("#e4e4e7"))
                    p.drawText(rect.adjusted(4, 2, -4, -2), Qt.AlignLeft | Qt.AlignTop, label)

        p.end()
        self._cache = pix

    def paintEvent(self, event):
        if self._cache is None or self._cache.size() != self.size():
            self._render_cache()
        if self._cache is not None:
            painter = QPainter(self)
            painter.drawPixmap(0, 0, self._cache)


class UserMessageEditor(QTextEdit):
    save_requested = Signal()
    save_and_retry_requested = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            modifiers = event.modifiers()
            if modifiers == Qt.NoModifier:
                self.save_requested.emit()
                event.accept()
                return
            if modifiers == Qt.ControlModifier:
                self.save_and_retry_requested.emit()
                event.accept()
                return
        # Shift+Enter still inserts a newline in a multiline request.
        from ui.markdown_input import handle_markdown_input
        if handle_markdown_input(self, event):
            event.accept()
            return
        super().keyPressEvent(event)


class UserMessageTurn(QFrame):
    delete_requested = Signal(object)
    retry_requested = Signal(object)
    create_branch_requested = Signal(object)
    content_edited = Signal()

    def __init__(
        self,
        text: str,
        timestamp: str = "",
        tags: Optional[List[str]] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        canvas_context: Optional[Dict[str, Any]] = None,
        file_context: Optional[Dict[str, Any]] = None,
        attachment_resolver=None,
        parent=None,
        display_text=None,
    ):
        super().__init__(parent)
        self.setObjectName("UserTurnCard")
        from ui.views.chat_references import turn_metadata
        turn_metadata(self)
        self.raw_text = text
        self.display_text = display_text
        self.tags = tags or []
        self.attachments = [dict(item) for item in (attachments or [])]
        self.canvas_context = dict(canvas_context or {})
        self.file_context = dict(file_context or {})
        self.attachment_resolver = attachment_resolver or (lambda _item: None)
        self.is_editing = False
        self.setMouseTracking(True)

        from ui.views.chat_presentation import UserTurnLayout
        outer_layout = UserTurnLayout(self)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Expanding)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.bubble = QFrame(self)
        self.bubble.setObjectName("UserMessageBubble")
        self.bubble.setAttribute(Qt.WA_StyledBackground, True)
        self.main_layout = QVBoxLayout(self.bubble)
        # Симметричные поля: положение текста не зависит от ручной поправки шрифта.
        self.main_layout.setContentsMargins(12, 8, 12, 8)
        self.main_layout.setAlignment(Qt.AlignVCenter)
        self.main_layout.setSpacing(4)
        outer_layout.addWidget(self.bubble)
        self.time_label = QLabel(timestamp or QTime.currentTime().toString("HH:mm"), self)
        self.time_label.hide()

        # Lightweight board projection. It is intentionally one cached-pixmap
        # widget, not an embedded CanvasView/QGraphicsScene.
        self.canvas_preview = None
        if self.canvas_context:
            self.canvas_preview = CanvasContextPreviewWidget(self.canvas_context, self)
            self.main_layout.addWidget(self.canvas_preview)

        # Текст запроса
        self.md_view = MarkdownMessageWidget(self)
        self.md_view.set_markdown(display_text if display_text is not None else text)
        if display_text is not None:
            bind_caption(self.md_view, text, setter='setToolTip')
        self.main_layout.addWidget(self.md_view)
        if not text:
            self.md_view.hide()

        self.attachments_view = MessageAttachmentsWidget(
            self.attachments,
            self.attachment_resolver,
            removable=True,
            parent=self,
        )
        self.attachments_view.attachment_removed.connect(self._remove_attachment)
        self.main_layout.addWidget(self.attachments_view)

        # Теги в сообщении используют тот же визуальный чип, что и активные
        # теги в панели выбора, и переносятся на следующую строку при нехватке места.
        self.tags_row = QWidget(self)
        self.tags_row.setObjectName("UserMessageTagsRow")
        self.tags_row.setAttribute(Qt.WA_StyledBackground, True)
        self.tags_row.setStyleSheet("QWidget#UserMessageTagsRow { background: transparent; border: none; }")
        self.tags_layout = FlowLayout(self.tags_row, margin=0, h_spacing=6, v_spacing=6)

        self._editing_tags = None
        self._refresh_tag_badges()
        self.main_layout.addWidget(self.tags_row)

        # Инлайн-редактор
        self.edit_box = QWidget(self)
        self.edit_box.setObjectName("ChatInlineEditorBox")
        edit_layout = QVBoxLayout(self.edit_box)
        edit_layout.setContentsMargins(0, 4, 0, 0)
        edit_layout.setSpacing(6)

        self.editor_text = UserMessageEditor(self.edit_box)
        self.editor_text.setObjectName("ChatInlineEditor")
        self.editor_text.save_requested.connect(self._save_edit)
        self.editor_text.save_and_retry_requested.connect(self._save_and_retry)
        edit_layout.addWidget(self.editor_text)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.save_btn = localize(QPushButton('', self.edit_box), 'ui.settings.save_b4d3')
        self.save_btn.setObjectName("ChatInlineSaveBtn")
        self.save_btn.clicked.connect(self._save_edit)
        self.save_retry_btn = localize(QPushButton('', self.edit_box), 'ui.chat_view.save_and_regenerate_e696')
        self.save_retry_btn.setObjectName("ChatInlineSaveBtn")
        self.save_retry_btn.clicked.connect(self._save_and_retry)
        self.cancel_btn = localize(QPushButton('', self.edit_box), 'ui.close_panel.cancel_8fbe')
        self.cancel_btn.setObjectName("ChatInlineCancelBtn")
        self.cancel_btn.clicked.connect(self._cancel_edit)
        btn_row.addWidget(self.cancel_btn)
        for button, shortcut in ((self.save_btn, "Enter"), (self.save_retry_btn, "Ctrl+Enter")):
            column = QVBoxLayout()
            column.setSpacing(3)
            column.addWidget(button)
            hint = QLabel(shortcut, self.edit_box)
            hint.setAlignment(Qt.AlignCenter)
            apply_style(hint, "color: @text.disabled@; font-size:11px; background: transparent;")
            column.addWidget(hint)
            btn_row.addLayout(column)
        edit_layout.addLayout(btn_row)

        self.edit_box.hide()
        self.main_layout.addWidget(self.edit_box)
        self.main_layout.removeWidget(self.tags_row)
        self.main_layout.addWidget(self.tags_row)
        from ui.views.chat_presentation import install_actions
        install_actions(self, "user")

    def enterEvent(self, event):
        self.actions_bar.refresh_visibility()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.actions_bar.refresh_visibility()
        super().leaveEvent(event)

    def _toggle_edit_mode(self):
        if not self.is_editing:
            self.editor_text.setPlainText(self.raw_text)
            self.md_view.hide()
            self._editing_tags = list(self.tags)
            self.edit_box.show()
            self.is_editing = True
            self._refresh_tag_badges()
            self.editor_text.setFocus()
            from ui.views.chat_presentation import focus_message_editor
            focus_message_editor(self)
        else:
            self._cancel_edit()

    def _save_edit(self):
        self.display_text = None
        self.chat_meta.pop("task_status_display", None)
        self.raw_text = self.editor_text.toPlainText().strip()
        if self._editing_tags is not None:
            self.tags = list(self._editing_tags)
        self._editing_tags = None
        self.md_view.set_markdown(self.raw_text)
        self.edit_box.hide()
        self.md_view.setVisible(bool(self.raw_text))
        self.is_editing = False
        self._editing_tags = None
        self._refresh_tag_badges()
        self.content_edited.emit()  # Мгновенная запись на диск

    def _save_and_retry(self):
        self._save_edit()
        self.retry_requested.emit(self)

    def _cancel_edit(self):
        self.edit_box.hide()
        self.md_view.setVisible(bool(self.raw_text))
        self.is_editing = False
        self._editing_tags = None
        self._refresh_tag_badges()

    def _refresh_tag_badges(self):
        while self.tags_layout.count():
            badge = self.tags_layout.takeAt(0).widget()
            if badge is not None:
                badge.hide()
                badge.deleteLater()
        tags = self._editing_tags if self._editing_tags is not None else self.tags
        for tag in tags:
            badge = TagBadgeWidget(tag, self.tags_row, removable=self.is_editing)
            badge.removed.connect(self._remove_editing_tag)
            self.tags_layout.addWidget(badge)
        from ui.views.chat_request_badges import resource_badges
        resources = resource_badges(self.chat_meta)
        for item in resources:
            kind = tr('ui.caption.b2ac851f000b') if item["kind"] == "skill" else tr('ui.module_view.pipeline_37e1')
            badge = TagBadgeWidget(kind + ": " + item["name"], self.tags_row)
            bind_caption(badge, kind + ": " + item["id"], setter='setToolTip')
            self.tags_layout.addWidget(badge)
        self.tags_row.setVisible(bool(tags or resources))
        self.tags_layout.invalidate()
        self.tags_row.updateGeometry()

    def _remove_editing_tag(self, tag):
        if not self.is_editing or self._editing_tags is None:
            return
        self._editing_tags = [value for value in self._editing_tags if value != tag]
        self._refresh_tag_badges()

    def _remove_attachment(self, attachment_id: str):
        attachment_id = str(attachment_id or "")
        before = len(self.attachments)
        self.attachments[:] = [
            item for item in self.attachments
            if str(item.get("id") or "") != attachment_id
        ]
        if len(self.attachments) != before:
            self.attachments_view.remove_attachment(attachment_id)
            # A user turn that consisted only of attachments must not leave an
            # empty shell in the chat after its last file is removed.
            if not self.attachments and not self.raw_text.strip() and not self.canvas_context:
                self.delete_requested.emit(self)
                return
            self.content_edited.emit()

    def _show_options_menu(self):
        menu = QMenu(self)
        menu.setObjectName("ChatMessageContextMenu")
        from ui.views.chat_references import add_reference_actions
        add_reference_actions(self, menu)
        act_select = localize(add_caption_action(menu, 'addAction', "Выделить сообщение"), 'ui.chat_references.select_message_e129')
        act_del = localize(add_caption_action(menu, 'addAction', "🗑️ Удалить навсегда"), 'ui.chat_view.delete_permanently_22d0')
        act_branch = localize(add_caption_action(menu, 'addAction', "🌿 Создать ветку"), 'ui.chat_view.create_branch_f261')
        act_retry = localize(add_caption_action(menu, 'addAction', "🔄 Повторить генерацию"), 'ui.chat_view.regenerate_1731')
        menu.addSeparator()
        act_copy_txt = localize(add_caption_action(menu, 'addAction', "📄 Копировать как текст"), 'ui.chat_view.copy_as_text_6089')
        act_copy_md = localize(add_caption_action(menu, 'addAction', "📝 Копировать как markdown"), 'ui.chat_view.copy_as_markdown_6d6c')

        action = menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height() + 2)))
        if action == act_select:
            from ui.views.chat_selection import select_from_menu
            select_from_menu(self)
        elif action == act_del:
            self.delete_requested.emit(self)
        elif action == act_branch:
            self.create_branch_requested.emit(self)
        elif action == act_retry:
            self.retry_requested.emit(self)
        elif action == act_copy_txt or action == act_copy_md:
            from ui.views.chat_selection import copy_from_menu
            copy_from_menu(self, action == act_copy_md)

class AssistantMessageTurn(QFrame):
    delete_requested = Signal(object)
    create_branch_requested = Signal(object)
    content_edited = Signal()

    def __init__(self, parent=None, segment_kind: str = "final"):
        super().__init__(parent)
        self.setObjectName("AssistantTurnCard")
        from ui.views.chat_references import turn_metadata
        turn_metadata(self)
        self.raw_content = ""
        self.segment_kind = str(segment_kind or "final")
        self.is_editing = False
        self.setMouseTracking(True)

        self.main_layout = QVBoxLayout(self)
        self.main_layout.setContentsMargins(8, 4, 8, 0)
        self.main_layout.setSpacing(6)

        self.time_label = QLabel("", self)
        self.time_label.hide()

        self.md_view = MarkdownMessageWidget(self)
        self.main_layout.addWidget(self.md_view)

        self.edit_box = QWidget(self)
        self.edit_box.setObjectName("ChatInlineEditorBox")
        edit_layout = QVBoxLayout(self.edit_box)
        edit_layout.setContentsMargins(0, 4, 0, 0)
        edit_layout.setSpacing(6)

        self.editor_text = form_field(QTextEdit(self.edit_box))
        self.editor_text.setObjectName("ChatInlineEditor")
        edit_layout.addWidget(self.editor_text)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        self.save_btn = localize(QPushButton('', self.edit_box), 'ui.settings.save_b4d3')
        self.save_btn.setObjectName("ChatInlineSaveBtn")
        self.save_btn.clicked.connect(self._save_edit)
        self.cancel_btn = localize(QPushButton('', self.edit_box), 'ui.close_panel.cancel_8fbe')
        self.cancel_btn.setObjectName("ChatInlineCancelBtn")
        self.cancel_btn.clicked.connect(self._cancel_edit)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.save_btn)
        edit_layout.addLayout(btn_row)

        self.edit_box.hide()
        self.main_layout.addWidget(self.edit_box)
        from ui.views.chat_presentation import install_actions
        install_actions(self, "assistant")

    def enterEvent(self, event):
        self.actions_bar.refresh_visibility()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.actions_bar.refresh_visibility()
        super().leaveEvent(event)

    def stamp_current_time(self):
        if not self.time_label.text():
            bind_caption(self.time_label, QTime.currentTime().toString("HH:mm"))

    def append_content(self, chunk: str):
        self.stamp_current_time()
        self.raw_content += chunk
        self.md_view.queue_markdown(self.raw_content)

    def _toggle_edit_mode(self):
        if not self.is_editing:
            self.editor_text.setPlainText(self.raw_content)
            self.md_view.hide()
            self.edit_box.show()
            self.is_editing = True
            from ui.views.chat_presentation import focus_message_editor
            focus_message_editor(self)
        else:
            self._cancel_edit()

    def _save_edit(self):
        self.raw_content = self.editor_text.toPlainText().strip()
        self.md_view.set_markdown(self.raw_content)
        self.edit_box.hide()
        self.md_view.show()
        self.is_editing = False
        self.content_edited.emit()

    def _cancel_edit(self):
        self.edit_box.hide()
        self.md_view.show()
        self.is_editing = False

    def _show_options_menu(self):
        menu = QMenu(self)
        menu.setObjectName("ChatMessageContextMenu")
        from ui.views.chat_references import add_reference_actions
        add_reference_actions(self, menu)
        act_select = localize(add_caption_action(menu, 'addAction', "Выделить сообщение"), 'ui.chat_references.select_message_e129')
        act_del = localize(add_caption_action(menu, 'addAction', "🗑️ Удалить навсегда"), 'ui.chat_view.delete_permanently_22d0')
        act_branch = localize(add_caption_action(menu, 'addAction', "🌿 Создать ветку"), 'ui.chat_view.create_branch_f261')
        menu.addSeparator()
        act_copy_txt = localize(add_caption_action(menu, 'addAction', "📄 Копировать как текст"), 'ui.chat_view.copy_as_text_6089')
        act_copy_md = localize(add_caption_action(menu, 'addAction', "📝 Копировать как markdown"), 'ui.chat_view.copy_as_markdown_6d6c')

        action = menu.exec(self.menu_btn.mapToGlobal(QPoint(0, self.menu_btn.height() + 2)))
        if action == act_select:
            from ui.views.chat_selection import select_from_menu
            select_from_menu(self)
        elif action == act_del:
            self.delete_requested.emit(self)
        elif action == act_branch:
            self.create_branch_requested.emit(self)
        elif action == act_copy_txt or action == act_copy_md:
            from ui.views.chat_selection import copy_from_menu
            copy_from_menu(self, action == act_copy_md)


# =========================================================================
# 7. ПОЛЕ ВВОДА
# =========================================================================

class AutoResizingTextEdit(InlineQuoteMixin, QTextEdit):
    quotes_changed = Signal()
    send_requested = Signal()
    attachment_paste_requested = Signal()

    @staticmethod
    def _is_send_key(event):
        from ui.chat_hotkeys import matches_send
        return matches_send(event)

    def event(self, event):
        # A window-level shortcut must not consume the composer's send key.
        if event.type() == QEvent.ShortcutOverride and self._is_send_key(event) and not getattr(self, '_ime_preedit', False):
            event.accept()
            return True
        return super().event(event)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatInputTextEdit")
        apply_style(self,'QTextEdit#ChatInputTextEdit {background:@background.panel@;border:0;padding:2px;}')
        self.viewport().setAutoFillBackground(True)
        self.viewport().setBackgroundRole(QPalette.Base)
        self.viewport().setAttribute(Qt.WA_OpaquePaintEvent,True)
        self.setAcceptRichText(False)
        self.setTabChangesFocus(False)

        self.min_height = 42
        self.max_height = 140
        self.setFixedHeight(self.min_height)
        self.textChanged.connect(self._adjust_height)
        self.init_inline_quotes()

    def inputMethodEvent(self, event):
        self._ime_preedit = bool(event.preeditString())
        super().inputMethodEvent(event)

    def paintEvent(self,event):
        # QTextEdit repaints only a narrow strip when the caret blinks. Paint
        # that dirty region opaquely before Qt draws the document and caret.
        painter = QPainter(self.viewport())
        painter.fillRect(event.rect(),theme_manager.color('background.panel'))
        painter.end()
        super().paintEvent(event)

    def _adjust_height(self):
        doc_height = int(self.document().size().height()) + 14
        new_height = max(self.min_height, min(doc_height, self.max_height))
        if self.height() != new_height:
            self.setFixedHeight(new_height)

    @staticmethod
    def _clipboard_has_attachment_payload() -> bool:
        mime = QGuiApplication.clipboard().mimeData()
        if not mime:
            return False
        from ui.markdown_clipboard import payload
        data = payload(mime)
        if data and data['images']:return True
        if mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            return True
        if mime.hasImage():
            return True
        return any(
            str(fmt).lower().split(";", 1)[0].startswith(("audio/", "video/"))
            for fmt in mime.formats()
        )

    def insertFromMimeData(self, mime):
        from ui.markdown_clipboard import payload
        data = payload(mime)
        if data and data['images']:
            self.attachment_paste_requested.emit()
            # The public text flavor already excludes image references.
            if mime.text():self.textCursor().insertText(mime.text())
            return
        if mime.hasImage() or (mime.hasUrls() and any(url.isLocalFile() for url in mime.urls())) or any(
                str(fmt).lower().split(';', 1)[0].startswith(('audio/', 'video/')) for fmt in mime.formats()):
            self.attachment_paste_requested.emit()
            return
        super().insertFromMimeData(mime)

    def contextMenuEvent(self, event):
        menu = self.createStandardContextMenu()
        if self._clipboard_has_attachment_payload():
            menu.addSeparator()
            attach_action = localize(add_caption_action(menu, 'addAction', "📎 Прикрепить из буфера обмена"), 'ui.chat_view.attach_from_clipboard_2a1f')
        else:
            attach_action = None
        chosen = menu.exec(event.globalPos())
        if attach_action is not None and chosen is attach_action:
            self.attachment_paste_requested.emit()
        menu.deleteLater()

    def keyPressEvent(self, event: QKeyEvent):
        if self._is_send_key(event) and not getattr(self, '_ime_preedit', False):
            if not event.isAutoRepeat():
                self.send_requested.emit()
            event.accept()
            return
        if self.quote_backspace(event):
            return
        if event.matches(QKeySequence.Paste) and self._clipboard_has_attachment_payload():
            self.insertFromMimeData(QGuiApplication.clipboard().mimeData())
            event.accept()
            return

        from ui.markdown_input import handle_markdown_input
        if handle_markdown_input(self, event):
            event.accept()
            return

        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            super().keyPressEvent(event)
            return

        # Пословный откат в чате
        if event.text() and event.text().isprintable() and not event.text().isspace() and not (event.modifiers() & (Qt.ControlModifier | Qt.AltModifier)):
            cursor = self.textCursor()
            if not cursor.hasSelection() and cursor.position() > 0 and self.document().characterAt(cursor.position() - 1).isspace():
                cursor.beginEditBlock()
                cursor.insertText(event.text())
                cursor.endEditBlock()
                event.accept()
                return

        super().keyPressEvent(event)


# =========================================================================
# 8. РЕЛЬС НАВИГАЦИИ И СКРОЛЛБАРА
# =========================================================================

class ChatTimelineRail(QWidget):
    user_scrolled = Signal()
    history_message_requested = Signal(dict)
    def __init__(self, scroll_area: QScrollArea, turns_container: QWidget, parent=None):
        super().__init__(parent)
        self.scroll_area = scroll_area
        self.turns_container = turns_container
        from ui.styles.binding import watch_paint
        watch_paint(self)
        self.setFixedWidth(18)
        self.setMouseTracking(True)
        self.user_turns: List[UserMessageTurn] = []
        self.history_markers = []
        self.lookup_turn = None
        self.loaded_turns = None
        self.loaded_from = None
        self.hovered_idx = -1
        self._is_dragging = False
        self._drag_offset_y = 0.0

        self.scroll_bar = self.scroll_area.verticalScrollBar()
        self.scroll_bar.valueChanged.connect(lambda _: self.update())
        self.scroll_bar.rangeChanged.connect(lambda _min, _max: self.update())

    def update_turns(self, turns: List[UserMessageTurn]):
        self.user_turns = [turn for turn in turns if isinstance(turn, UserMessageTurn)]
        self.update()

    def set_history_markers(self, markers):
        from core.chat_references import valid_chat_id
        seen = set()
        self.history_markers = []
        for marker in markers:
            identity = marker.get('chat_id')
            position = marker.get('position')
            if (marker.get('role') in {'human', 'user'} and valid_chat_id(identity)
                    and type(position) is int and position >= 0 and identity not in seen):
                self.history_markers.append(marker)
                seen.add(identity)
        self.hovered_idx = -1
        self.update()

    def _markers(self):
        turns = [turn for turn in (self.loaded_turns() if self.loaded_turns else self.user_turns)
                 if isinstance(turn, UserMessageTurn)]
        if not self.history_markers:
            return [{'turn': turn} for turn in turns]
        markers = list(self.history_markers)
        boundary = self.loaded_from() if self.loaded_from else None
        if boundary is not None:
            loaded_ids = {getattr(turn, 'chat_meta', {}).get('chat_id') for turn in turns}
            # A removed loaded message must not leave a dead dot. Older pages
            # retain their metadata until they are actually loaded.
            markers = [item for item in markers if item['position'] < boundary or item['chat_id'] in loaded_ids]
        known = {item.get('chat_id') for item in markers if item.get('chat_id')}
        for turn in turns:
            identity = getattr(turn, 'chat_meta', {}).get('chat_id')
            if identity and identity not in known:
                markers.append({'turn': turn, 'chat_id': identity})
                known.add(identity)
        return markers

    def _turn_for_marker(self, marker):
        turn = marker.get('turn')
        if turn is not None:
            return turn
        identity = marker.get('chat_id')
        return self.lookup_turn(identity) if identity and self.lookup_turn else None

    def _get_slider_geometry(self) -> Tuple[float, float]:
        H = float(self.height())
        v_max = self.scroll_bar.maximum()
        v_val = self.scroll_bar.value()
        page = self.scroll_bar.pageStep()
        total = v_max + page

        if total <= 0 or v_max <= 0:
            return 0.0, H

        thumb_h = max(28.0, (page / float(total)) * H)
        thumb_h = min(thumb_h, H)
        travel = H - thumb_h
        thumb_y = (float(v_val) / float(v_max)) * travel if travel > 0 else 0.0
        return thumb_y, thumb_h

    def _get_dot_positions(self) -> List[float]:
        h = float(self.height())
        count = len(self._markers())
        if count == 0:
            return []
        if count == 1:
            return [h / 2.0]
        step = (h - 40.0) / float(count - 1)
        return [20.0 + i * step for i in range(count)]

    def paintEvent(self, event):
        if not self._markers() and self.scroll_bar.maximum() <= 0:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        w = float(self.width())
        h = float(self.height())
        cx = w / 2.0

        painter.setPen(QPen(theme_manager.color('border.subtle'), 1))
        painter.drawLine(QPointF(cx, 10), QPointF(cx, h - 10))

        thumb_y, thumb_h = self._get_slider_geometry()
        slider_rect = QRectF(cx - 2.0, thumb_y, 4.0, thumb_h)
        slider_color = theme_manager.color('accent.primary' if self._is_dragging or self.underMouse() else 'text.disabled')
        painter.setBrush(QBrush(slider_color))
        painter.setPen(Qt.NoPen)
        painter.drawRoundedRect(slider_rect, 2, 2)

        positions = self._get_dot_positions()
        for i, cy in enumerate(positions):
            is_hover = (i == self.hovered_idx)
            r = 5.5 if is_hover else 4.5
            dot_color = theme_manager.color('accent.primary' if is_hover else 'text.disabled')
            painter.setBrush(QBrush(dot_color))
            painter.setPen(Qt.NoPen)
            painter.drawEllipse(QRectF(cx - r, cy - r, r * 2.0, r * 2.0))

    def _get_hit_dot_index(self, pos_y: float) -> int:
        positions = self._get_dot_positions()
        if not positions:
            return -1
        index = min(range(len(positions)), key=lambda i: abs(pos_y - positions[i]))
        return index if abs(pos_y - positions[index]) <= 9.0 else -1

    def mouseMoveEvent(self, event: QMouseEvent):
        pos_y = event.position().y()
        thumb_y, thumb_h = self._get_slider_geometry()

        if self._is_dragging:
            travel = float(self.height()) - thumb_h
            if travel > 0:
                target_top = max(0.0, min(pos_y - self._drag_offset_y, travel))
                ratio = target_top / travel
                self.scroll_bar.setValue(int(round(ratio * self.scroll_bar.maximum())))
            self.user_scrolled.emit()
            self.update()
            return

        dot_idx = self._get_hit_dot_index(pos_y)
        if dot_idx != self.hovered_idx:
            self.hovered_idx = dot_idx
            self.update()

        markers = self._markers()
        if 0 <= dot_idx < len(markers):
            turn = self._turn_for_marker(markers[dot_idx])
            if turn is not None:
                snippet = str(getattr(turn, 'raw_text', getattr(turn, 'raw_content', ''))).strip().replace("\n", " ")
                QToolTip.showText(event.globalPosition().toPoint(), snippet[:60] + ('…' if len(snippet) > 60 else ''), self)
            else:
                QToolTip.hideText()
            self.setCursor(Qt.PointingHandCursor)
        elif thumb_y <= pos_y <= (thumb_y + thumb_h):
            QToolTip.hideText()
            self.setCursor(Qt.SizeVerCursor)
        else:
            QToolTip.hideText()
            self.setCursor(Qt.ArrowCursor)

        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hovered_idx = -1
        QToolTip.hideText()
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            pos_y = event.position().y()
            dot_idx = self._get_hit_dot_index(pos_y)

            markers = self._markers()
            if 0 <= dot_idx < len(markers):
                turn = self._turn_for_marker(markers[dot_idx])
                if turn is None:
                    self.history_message_requested.emit(markers[dot_idx])
                    event.accept()
                    return
                from ui.views.chat_references import controller_for
                references = controller_for(turn)
                if references is not None:
                    references.jump(turn, align='top')
                else:
                    target_y = turn.mapTo(self.turns_container, QPoint(0, 0)).y()
                    self.scroll_bar.setValue(target_y - 10)
                event.accept()
                self.user_scrolled.emit()
                return

            thumb_y, thumb_h = self._get_slider_geometry()
            travel = float(self.height()) - thumb_h

            if thumb_y <= pos_y <= (thumb_y + thumb_h):
                self._is_dragging = True
                self._drag_offset_y = pos_y - thumb_y
            else:
                if travel > 0:
                    target_top = max(0.0, min(pos_y - thumb_h / 2.0, travel))
                    ratio = target_top / travel
                    self.scroll_bar.setValue(int(round(ratio * self.scroll_bar.maximum())))
                self._is_dragging = True
                self._drag_offset_y = thumb_h / 2.0

            self.update()
            self.user_scrolled.emit()
            event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent):
        self._is_dragging = False
        self.update()
        event.accept()


# =========================================================================
# 9. ГЛАВНАЯ СТУДИЯ ЧАТА
# =========================================================================

from ui.session_history import SessionHistoryMixin


from ui.views.chat_subagents import SubagentMessageTurn
from ui.views.chat_notifications import NotificationTurn, render_notification


class ChatView(SessionHistoryMixin, QWidget):
    system_prompt_requested = Signal(str, str)
    compressed_context_requested = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatViewStudioRoot")
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.timeout.connect(self._sync_chat_overlays)
        self.session_manager = SessionApiClient(base_url=core_process.base_url)
        self.session_load_runner = AsyncRunner(self, max_threads=2)
        self._session_load_generation = 0
        self._session_load_guard = {"generation": 0}
        self._session_loading = False
        self._model_change_pending = False
        self._init_session_history()
        self._session_render_messages = None
        self._session_render_timer = QTimer(self)
        self._session_render_timer.setSingleShot(True)
        self._session_render_timer.timeout.connect(self._render_session_batch)
        self.session_write_runner = AsyncRunner(self, max_threads=1)
        self.attachment_runner = AsyncRunner(self, max_threads=1)
        self.task_status_runner = AsyncRunner(self, max_threads=1)
        self._pending_attachments: List[Dict[str, Any]] = []
        self._preparing_attachments = False
        self.setAcceptDrops(True)
        self.current_session_id: Optional[str] = None
        self.current_branch: str = "main"
        self.current_workspace_root: Optional[str] = None
        self.current_temp_memory_mode: str = "text"
        self.current_include_thoughts_in_context: bool = True
        self._pending_new_session_folder_id: Optional[str] = None

        self.messages_history: List[Dict[str, str]] = []
        self.current_worker: Optional[ChatStreamWorker] = None
        self._active_runs = {}
        self._detached_runs = set()
        self.active_thought_turn: Optional[ThoughtMessageTurn] = None
        self.active_assistant_turn: Optional[AssistantMessageTurn] = None
        self._pending_assistant_identity = None
        self._pending_assistant_prefix = ""
        self._generation_insert_index: Optional[int] = None
        self._retry_mode_active = False
        self._pending_retry = None
        self.user_turn_widgets: List[UserMessageTurn] = []
        self._embedding_errors_seen = set()
        self._embedding_warning_message = ""
        self._session_sync_retry_timer = QTimer(self)
        self._session_sync_retry_timer.setSingleShot(True)
        self._session_sync_retry_timer.setInterval(1500)
        self._session_sync_retry_timer.timeout.connect(self._sync_session_to_disk)

        main_layout = QHBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 4, 0)
        main_layout.setSpacing(0)

        center_col = QWidget(self)
        self.center_col = center_col
        center_col.setAcceptDrops(True)
        center_col.setMinimumWidth(550)
        center_col.setMaximumWidth(950)
        center_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        col_layout = QVBoxLayout(center_col)
        col_layout.setContentsMargins(16, 16, 16, 16)
        col_layout.setSpacing(14)

        self.welcome_widget = ChatWelcomeWidget(center_col)
        self.welcome_area = QScrollArea(center_col)
        self.welcome_area.setObjectName('ChatWelcomeArea')
        self.welcome_area.setFrameShape(QFrame.NoFrame)
        self.welcome_area.setWidgetResizable(True)
        self.welcome_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.welcome_area.setMinimumHeight(0)
        self.welcome_area.setWidget(self.welcome_widget)
        col_layout.addWidget(self.welcome_area, 1)

        self.scroll_area = ChatScrollArea(center_col)
        self.scroll_area.older_requested.connect(self._on_history_wheel)
        self.scroll_area.navigation_started.connect(self._on_user_scroll_started)
        self.scroll_area.viewport().setAcceptDrops(True)

        self._auto_scroll_to_bottom = True
        self._user_scroll_pending = False
        self._user_scroll_timer = QTimer(self)
        self._user_scroll_timer.setSingleShot(True)
        self._user_scroll_timer.timeout.connect(self._finish_user_scroll)
        self._thought_scroll_anchor = None
        self._thought_anchor_passes = 0
        self._thought_anchor_timer = QTimer(self)
        self._thought_anchor_timer.setSingleShot(True)
        self._thought_anchor_timer.timeout.connect(self._settle_thought_position)
        v_bar = self.scroll_area.verticalScrollBar()
        v_bar.rangeChanged.connect(self._on_scroll_range_changed)
        v_bar.valueChanged.connect(self._on_scroll_value_changed)
        v_bar.sliderMoved.connect(self._on_history_slider_moved)

        self.turns_container = QWidget()
        self.turns_container.setObjectName("ChatTurnsContainer")
        from ui.views.chat_selection import ChatTurnsLayout
        self.turns_layout = ChatTurnsLayout(self.turns_container)
        self.turns_layout.setContentsMargins(0, 0, 0, 0)
        self.turns_layout.setSpacing(0)
        self.turns_layout.addStretch(1)

        self.scroll_area.setWidget(self.turns_container)
        col_layout.addWidget(self.scroll_area, 1)
        self.scroll_area.hide()

        # Панель тегов — overlay. Она намеренно НЕ входит в основной layout:
        # открытие тегов не должно сдвигать welcome/memory controls до старта сессии.
        from ui.session_input import ScopedTagsState
        self.tags_bar = TagsBarWidget(center_col, state=ScopedTagsState(shared_tags_state, self))
        self.tags_bar.installEventFilter(self)
        self.tags_bar.tag_state_changed.connect(self._schedule_overlay_update)
        self.tags_bar.hide()

        self.input_card = QFrame(center_col)
        self.input_card.setObjectName("ChatStudioInputCard")
        input_layout = QVBoxLayout(self.input_card)
        input_layout.setContentsMargins(12, 8, 12, 8)
        input_layout.setSpacing(8)

        self.input_edit = AutoResizingTextEdit(self.input_card)
        bind_caption(self.input_edit, tr("chat_placeholder", "Задайте вопрос, отправьте задачу или код..."), setter='setPlaceholderText')
        self.input_edit.send_requested.connect(self._send_message)
        self.input_edit.attachment_paste_requested.connect(self._paste_attachments_from_clipboard)
        input_layout.addWidget(self.input_edit)

        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 0, 0, 0)
        bottom_bar.setSpacing(6)

        from ui.views.chat_input_controls import ChatIconButton, ChatInputControls, ChatSendButton
        self.add_btn = ChatIconButton('plus', tr('ui.chat_view.add_attachment_3dba'), self.input_card)
        self.add_btn.setObjectName("ChatInputAuxBtn")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(lambda: self._choose_attachment_files(tr('nav.file'), tr('ui.caption.290a46163f4d')))

        self.tags_btn = ChatIconButton('tag', tr('ui.chat_view.request_tags_0aa7'), self.input_card)
        self.tags_btn.setObjectName("ChatInputAuxBtn")
        self.tags_btn.setCursor(Qt.PointingHandCursor)
        self.tags_btn.clicked.connect(self._toggle_tags_panel)

        from ui.widgets.pipeline_selector import PipelineSelector
        self.pipeline_selector = PipelineSelector(self.input_card)
        self.pipeline_selector.selection_changed.connect(self._pipeline_selected)
        from ui.widgets.model_selector import ModelSelector
        self.model_selector = ModelSelector(self.input_card, before_change=self._confirm_session_model_change)
        self.model_selector.selection_changed.connect(self._model_profile_selected)
        self._pipeline_dialogs = {}
        self._pipeline_recovery_id = None
        self.pipeline_continue_btn = localize(QPushButton('', self.input_card), 'ui.chat_view.continue_pipeline_3dba')
        self.pipeline_continue_btn.setObjectName("ChatContinueBtn")
        self.pipeline_continue_btn.clicked.connect(lambda: [d.show() for d in self._pipeline_dialogs.values()])
        self.pipeline_continue_btn.hide()
        self._pipeline_poll = QTimer(self)
        self._pipeline_poll.setInterval(3000)
        self._pipeline_poll.timeout.connect(self._poll_pipeline_pauses)
        self._pipeline_poll.start()

        self.sys_prompt_btn = localize(QPushButton('', self.input_card), 'ui.chat_view.sys_prompt_0974')
        self.sys_prompt_btn.setObjectName("ChatInputAuxBtn")
        self.sys_prompt_btn.setCursor(Qt.PointingHandCursor)
        set_text(self.sys_prompt_btn, 'ui.chat_view.view_hidden_model_context_system_prompts_e630', setter='setToolTip')
        self.sys_prompt_btn.clicked.connect(self._request_system_prompt_preview)
        self.compressed_context_btn = localize(QPushButton('', self.input_card), 'ui.chat_input_controls.view_compacted_context_2ef5')
        self.compressed_context_btn.clicked.connect(lambda: self.compressed_context_requested.emit(
            self.current_session_id or '', self.current_branch or 'main'))
        self.raw_request_btn=localize(QPushButton('',self.input_card), 'ui.chat_input_controls.clean_request_ad1d')
        self.raw_request_btn.setObjectName('ChatInputAuxBtn')
        self.raw_request_btn.setCheckable(True)
        set_text(self.raw_request_btn, 'ui.chat_view.only_the_current_request_text_no_6766', setter='setToolTip')
        apply_style(self.raw_request_btn, 'QPushButton:checked {background:#153040;border:1px solid @accent.primary@;color:#b8e5ff;}')
        self.raw_request_btn.toggled.connect(lambda _:self._set_attachment_preparing(self._preparing_attachments))

        # Ненавязчивый индикатор фоновой проблемы embeddings. Он не является
        # ошибкой текущего запроса: подробности доступны по hover, а большой
        # banner показывается только если реальный memory/vector tool упал.
        self.embedding_warning_btn = QToolButton(self.input_card)
        set_icon(self.embedding_warning_btn, 'warning', square=True)
        bind_caption(self.embedding_warning_btn, "", setter='setToolTip')
        self.embedding_warning_btn.setCursor(Qt.ArrowCursor)
        self.embedding_warning_btn.setFixedSize(20, 20)
        apply_style(self.embedding_warning_btn, 'QToolButton { color:#fbbf24; background:transparent; border:1px solid #6b5317; border-radius:10px; font-size:13px; font-weight:bold; padding:0px; }QToolButton:hover { background:@control.hover@; border-color:#a77b18; }')
        self.embedding_warning_btn.hide()

        from ui.chat_hotkeys import send_label
        self.send_btn = ChatSendButton('send', send_label(), self.input_card)
        event_bus.chat_send_shortcut_changed.connect(self._refresh_send_shortcut_caption)
        i18n.language_changed.connect(self._refresh_send_shortcut_caption)
        self.send_btn.setObjectName("ChatStudioSendBtn")
        self.send_btn.setCursor(Qt.PointingHandCursor)
        self.send_btn.clicked.connect(self._send_message)
        self.stop_run_btn = QPushButton(self.input_card)
        self.stop_run_btn.setObjectName('ChatInputAuxBtn')
        set_text(self.stop_run_btn, 'ui.chat_view.stop_generation_c96d', setter='setToolTip')
        self.stop_run_btn.clicked.connect(self._stop_current_run)
        self.stop_run_btn.hide()
        self.stop_thinking_btn = QPushButton(self.input_card)
        self.stop_thinking_btn.setObjectName('ChatInputAuxBtn')
        self.stop_thinking_btn.setCursor(Qt.PointingHandCursor)
        self.stop_thinking_btn.setEnabled(False)
        set_text(self.stop_thinking_btn, 'ui.chat_view.finish_the_current_work_and_provide_31fd', setter='setToolTip')
        self.stop_thinking_btn.clicked.connect(self._stop_thinking)
        from ui.widgets.generation_button import configure_generation_button
        configure_generation_button(self.stop_run_btn, self.send_btn, thinking=False)
        configure_generation_button(self.stop_thinking_btn, self.send_btn, thinking=True)
        self.reasoning_control_runner = AsyncRunner(self, max_threads=1)
        self._thinking_control_pending = None

        bottom_bar.addWidget(self.add_btn)
        bottom_bar.addWidget(self.tags_btn)
        self.input_controls = ChatInputControls(self)
        bottom_bar.addWidget(self.input_controls.tools_button)
        bottom_bar.addWidget(self.input_controls.info_button)
        from ui.widgets.reasoning_selector import ReasoningSelector
        self.reasoning_selector = ReasoningSelector(self)
        bottom_bar.addWidget(self.reasoning_selector)
        bottom_bar.addWidget(self.model_selector)
        bottom_bar.addWidget(self.input_controls.model_tile)
        bottom_bar.addWidget(self.embedding_warning_btn)
        bottom_bar.addStretch(1)
        from ui.plugin_host import populate_slot
        populate_slot('ui.chat.input.actions', bottom_bar, parent=self.input_card, data=self)
        bottom_bar.addWidget(self.stop_run_btn)
        bottom_bar.addWidget(self.stop_thinking_btn)
        bottom_bar.addWidget(self.send_btn)
        input_layout.addLayout(bottom_bar)
        input_layout.addWidget(self.pipeline_continue_btn)

        col_layout.addWidget(self.input_card)

        # Pending attachments are an overlay above the input card. The error
        # panel is anchored above this panel, so runtime errors never cover it.
        self.attachments_panel = PendingAttachmentsPanel(center_col)
        self.attachments_panel.remove_requested.connect(self._remove_pending_attachment)
        self.attachments_panel.hide()

        # Transient error banner: an overlay anchored immediately above the
        # input card. It is deliberately outside the message layout so repeated
        # runtime/embedding errors cannot push or reflow the chat history.
        # Do not reuse SystemMessageStudioLabel here: that stylesheet has a
        # 20 px horizontal margin intended for in-history messages.
        self.error_panel = QFrame(center_col)
        self.error_panel.setObjectName("TransientErrorPanel")
        apply_style(self.error_panel, '#TransientErrorPanel { color: #fbbf24; background-color: @background.panel@; border: 1px solid @border.subtle@; border-radius: 6px; margin: 0px;}')
        error_layout = QHBoxLayout(self.error_panel)
        error_layout.setContentsMargins(14, 8, 8, 8)
        error_layout.setSpacing(8)

        self.error_panel_label = QLabel("", self.error_panel)
        self.error_panel_label.setAlignment(Qt.AlignCenter)
        self.error_panel_label.setWordWrap(True)
        self.error_panel_label.setStyleSheet(
            "color: #fbbf24; background: transparent; border: none; font-size:12px;"
        )
        self.error_panel_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        error_layout.addWidget(self.error_panel_label, 1)

        from ui.widgets.error_actions import copy_error_button
        self.error_panel_copy = copy_error_button(self.error_panel, lambda: getattr(self, '_error_text', ''))
        error_layout.addWidget(self.error_panel_copy, 0, Qt.AlignVCenter)
        self.error_panel_close = QToolButton(self.error_panel)
        set_icon(self.error_panel_close, 'close', square=True)
        set_text(self.error_panel_close, 'ui.top_bar.close_a7a4', setter='setToolTip')
        self.error_panel_close.setCursor(Qt.PointingHandCursor)
        self.error_panel_close.setFixedSize(28, 28)
        apply_style(self.error_panel_close, 'QToolButton { color:#d6a82d; background:transparent; border:none; font-size:21px; padding:0px; }QToolButton:hover { color:@text.primary@; background:@control.hover@; border-radius:4px; }')
        self.error_panel_close.clicked.connect(self._clear_error_panel)
        error_layout.addWidget(self.error_panel_close, 0, Qt.AlignVCenter)
        self.error_panel.hide()
        from ui.views.context_offer import ContextCompressionOffer
        self.context_offer = ContextCompressionOffer(self)

        self.input_card.installEventFilter(self)
        self.center_col.installEventFilter(self)
        self.welcome_widget.installEventFilter(self)

        self.timeline_rail = ChatTimelineRail(self.scroll_area, self.turns_container, self)
        self.timeline_rail.user_scrolled.connect(self._on_history_wheel)
        self.timeline_rail.user_scrolled.connect(self._on_user_scroll_started)
        self.timeline_rail.history_message_requested.connect(self.jump_to_history_message)
        self.timeline_rail.lookup_turn = lambda identity: self.chat_references.lookup(identity)
        self.timeline_rail.loaded_turns = lambda: [turn for turn in self.chat_references.turns()
            if isinstance(turn, UserMessageTurn)]
        self.timeline_rail.loaded_from = lambda: (None if self._session_loading or self._history_prepending
                                                 else self._history_before)
        self.timeline_rail.hide()
        self.scroll_area.installEventFilter(self)
        from ui.views.chat_selection import MessageSelection
        self.message_selection = MessageSelection(self, col_layout)
        from ui.views.chat_references import ChatReferences
        self.chat_references = ChatReferences(self, col_layout)
        from ui.views.chat_search import ChatSearch
        self.chat_search = ChatSearch(self, self.chat_references.search_button)

        # Keep the chat column centered. The timeline rail is an overlay placed
        # just outside the message area, so it does not push the chat off-center.
        main_layout.addStretch(1)
        main_layout.addWidget(center_col, 10)
        main_layout.addStretch(1)

        event_bus.session_selected.connect(self.open_session)
        event_bus.new_chat_requested.connect(self.reset_to_new_chat)
        event_bus.new_chat_in_folder_requested.connect(self.reset_to_new_chat)
        event_bus.session_renamed.connect(self._on_session_renamed)
        event_bus.branch_renamed.connect(self._on_branch_renamed)
        event_bus.session_memory_mode_changed.connect(self._on_session_memory_mode_changed)
        event_bus.session_thoughts_context_changed.connect(self._on_session_thoughts_context_changed)
        event_bus.session_replied_thoughts_context_changed.connect(self._on_session_replied_thoughts_context_changed)
        i18n.language_changed.connect(self._retranslate)
        from ui.session_input import SessionInputState
        self.input_state = SessionInputState(self)
        self._closing_workers = []
        from ui.shutdown import on_shutdown
        on_shutdown(self.shutdown_runs, wait=self.wait_for_runs)

    def _composer_rect(self):
        selection = getattr(self, 'message_selection', None)
        widget = selection.toolbar_holder if selection is not None and selection.active else self.input_card
        return QRect(widget.mapTo(self.center_col, QPoint()), widget.size())

    def _position_attachments_panel(self):
        if not hasattr(self, "attachments_panel") or not self.attachments_panel.isVisible():
            return
        composer = self._composer_rect()
        width = max(1, composer.width())
        panel_height = self.attachments_panel.preferred_height(width)
        x = composer.x()
        anchor_y = composer.y()
        if hasattr(self, 'chat_references') and self.chat_references.reply_bar.isVisible():
            anchor_y = self.chat_references.reply_overlay_top()
        y = max(0, anchor_y - panel_height - 8)
        self.attachments_panel.setGeometry(x, y, width, panel_height)
        self.attachments_panel.raise_()

    def _position_error_panel(self):
        if not hasattr(self, "error_panel") or not self.error_panel.isVisible():
            return

        anchor = self._composer_rect()
        width = max(1, anchor.width())
        x = anchor.x()

        self.error_panel.setFixedWidth(width)
        text_width = max(120, width - 14 - 8 - 16 - self.error_panel_close.width() - self.error_panel_copy.width())
        text_height = self.error_panel_label.heightForWidth(text_width)
        panel_height = max(40, min(190, text_height + 16))
        anchor_y = anchor.y()
        if hasattr(self, 'chat_references') and self.chat_references.reply_bar.isVisible():
            anchor_y = self.chat_references.reply_overlay_top()
        if hasattr(self, "attachments_panel") and self.attachments_panel.isVisible():
            anchor_y = self.attachments_panel.y()
        y = max(0, anchor_y - panel_height - 8)
        self.error_panel.setGeometry(x, y, width, panel_height)
        self.error_panel.raise_()

    def _show_error_panel(self, text: str):
        if not hasattr(self, "error_panel"):
            return
        message = str(text or tr('ui.caption.2d55ddef0ce5')).strip()
        self._error_text = message
        self.error_panel_label.setTextFormat(Qt.PlainText)
        bind_caption(self.error_panel_label, message)
        self.error_panel.show()
        self._position_attachments_panel()
        self._position_error_panel()
        # Tags are another overlay in the same area; when both are visible,
        # keep tags above the error banner instead of letting them overlap.
        self._position_tags_bar()
        self._schedule_overlay_update()

    def _clear_error_panel(self):
        self._embedding_errors_seen.clear()
        if hasattr(self, "error_panel"):
            self.error_panel.hide()
        if hasattr(self, "error_panel_label"):
            self.error_panel_label.clear()
        self._schedule_overlay_update()

    def _position_tags_bar(self):
        if not hasattr(self, "tags_bar") or not self.tags_bar.isVisible():
            return
        composer = self._composer_rect()
        width = max(1, composer.width())
        layout_height = self.tags_bar.flow_layout.heightForWidth(width)
        panel_height = max(36, min(170, layout_height))
        x = composer.x()
        anchor_y = composer.y()
        if hasattr(self, 'chat_references') and self.chat_references.reply_bar.isVisible():
            anchor_y = self.chat_references.reply_overlay_top()
        if hasattr(self, "error_panel") and self.error_panel.isVisible():
            anchor_y = self.error_panel.y()
        elif hasattr(self, "attachments_panel") and self.attachments_panel.isVisible():
            anchor_y = self.attachments_panel.y()
        y = max(0, anchor_y - panel_height - 8)
        self.tags_bar.setGeometry(x, y, width, panel_height)
        self.tags_bar.raise_()

    def _position_timeline_rail(self):
        if not hasattr(self, "timeline_rail") or not hasattr(self, "scroll_area"):
            return
        if not self.scroll_area.isVisible():
            self.timeline_rail.hide()
            return

        gap = 8
        top_left = self.scroll_area.mapTo(self, QPoint(0, 0))
        x = top_left.x() + self.scroll_area.width() + gap
        x = max(0, min(x, self.width() - self.timeline_rail.width()))
        self.timeline_rail.setGeometry(
            x,
            top_left.y(),
            self.timeline_rail.width(),
            self.scroll_area.height(),
        )
        self.timeline_rail.show()
        self.timeline_rail.raise_()
        search = getattr(self,'chat_search',None)
        if search is not None and search.popup.isVisible():search.popup.raise_()

    def _request_system_prompt_preview(self):
        self.system_prompt_requested.emit(self.current_session_id or "", self.current_branch or "main")

    def _confirm_session_model_change(self, profile_id):
        if self._model_change_pending:
            return False
        self._model_profile_previous = self.model_selector.selected
        old = self.model_selector.profile_fingerprint(self.model_selector.resolved_profile_id())
        new = self.model_selector.profile_fingerprint(profile_id)
        if not self.current_session_id or old == new:
            return True
        box = QMessageBox(self)
        set_text(box, 'ui.chat_view.change_session_model_a232', setter='setWindowTitle')
        box.setIcon(QMessageBox.Warning)
        set_text(box, 'ui.chat_view.changing_the_model_will_clear_the_cd25')
        switch = box.addButton(tr('ui.caption.6c5a2b08499a'), QMessageBox.DestructiveRole)
        cancel = box.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        return box.clickedButton() is switch

    def _model_profile_selected(self, profile_id):
        if self.current_session_id:
            session, branch = self.current_session_id, self.current_branch
            previous = self._model_profile_previous
            self._model_change_pending = True
            self.model_selector.setEnabled(False)

            def done(result):
                self._model_change_pending = False
                self.model_selector.setEnabled(not self._session_loading)
                if (session, branch) == (self.current_session_id, self.current_branch):
                    self._session_metadata = dict(getattr(self, '_session_metadata', {}), model_profile_id=profile_id)
                event_bus.session_model_changed.emit(session)

            def failed(error):
                self._model_change_pending = False
                self.model_selector.setEnabled(not self._session_loading)
                if (session, branch) == (self.current_session_id, self.current_branch):
                    self.model_selector.set_profile(previous)
                self._show_error_panel(str(error))

            self.session_write_runner.submit(lambda: self.session_manager.update_metadata(
                session, {"model_profile_id": profile_id}, branch=branch),
                on_success=done, on_error=failed)

    def _pipeline_selected(self, pipeline_id):
        if hasattr(self, 'input_state'):
            self.input_state.changed()

    def _show_pipeline_pause(self, payload):
        from ui.widgets.pipeline_selector import PipelinePauseDialog
        request_id = payload['request_id']
        existing = self._pipeline_dialogs.get(request_id)
        if existing is not None:
            self.pipeline_continue_btn.show()
            return
        dialog = PipelinePauseDialog(payload, self)
        self._pipeline_dialogs[request_id] = dialog
        self.pipeline_continue_btn.show()
        def continued():
            self._pipeline_dialogs.pop(request_id, None)
            self._pipeline_recovery_id = payload.get('run_id')
        dialog.accepted.connect(continued)
        dialog.show()

    def _poll_pipeline_pauses(self):
        if not self.current_session_id or not core_process.is_running():
            return
        import httpx
        from core.security import auth_headers, load_or_create_core_token
        session = self.current_session_id
        def fetch():
            with httpx.Client(timeout=3) as client:
                r = client.get(core_process.base_url + '/v1/pipelines/pending', params={'session_id': session},
                    headers=auth_headers(load_or_create_core_token()))
                r.raise_for_status(); return r.json()
        def loaded(result):
            if self.current_session_id != session:
                return
            requests = [r for r in result.get('requests', []) if r.get('branch', 'main') == self.current_branch]
            self.pipeline_continue_btn.setVisible(bool(requests))
            for request in requests:
                self._show_pipeline_pause(request)
            active = {r['request_id'] for r in requests}
            for rid in list(self._pipeline_dialogs):
                if rid not in active:
                    panel = self._pipeline_dialogs.pop(rid)
                    panel.close()
                    panel.deleteLater()
            if self._pipeline_recovery_id and self._pipeline_recovery_id not in result.get('active_runs', []):
                self._pipeline_recovery_id = None
                if not self.current_worker or not self.current_worker.isRunning():
                    self.load_session(session, self.current_branch)
        self.task_status_runner.submit(fetch, loaded)

    def _toggle_tags_panel(self):
        will_show = not self.tags_bar.isVisible()
        self.tags_bar.setVisible(will_show)
        self.tags_btn.set_active(will_show)
        self.input_state.changed()
        if will_show:
            self._position_tags_bar()
            self._schedule_overlay_update()

    def eventFilter(self, watched, event):
        drop_targets = (
            getattr(self, "center_col", None),
            getattr(getattr(self, "scroll_area", None), "viewport", lambda: None)(),
        )
        if watched in drop_targets and event.type() in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            mime = event.mimeData()
            paths = [
                url.toLocalFile()
                for url in (mime.urls() if mime and mime.hasUrls() else [])
                if url.isLocalFile()
            ]
            if paths and not self._preparing_attachments and not self._session_loading:
                if event.type() == QEvent.Drop:
                    self._queue_attachment_paths(paths)
                event.acceptProposedAction()
                return True

        if watched is getattr(self, 'welcome_widget', None) and event.type() in (QEvent.Show, QEvent.Hide):
            self._schedule_overlay_update()
        if watched is getattr(self, 'tags_bar', None) and hasattr(self, 'tags_btn'):
            if event.type() in (QEvent.Show, QEvent.Hide) and isValid(self.tags_btn):
                self.tags_btn.set_active(event.type() == QEvent.Show)
        if watched in (getattr(self, "input_card", None), getattr(self, "center_col", None)):
            if event.type() in (QEvent.Resize, QEvent.Move):
                self._schedule_overlay_update()
        if watched is getattr(self, "scroll_area", None):
            if event.type() in (QEvent.Resize, QEvent.Move, QEvent.Show, QEvent.Hide):
                self._schedule_overlay_update()
        return super().eventFilter(watched, event)

    def _schedule_overlay_update(self):
        if not self._overlay_timer.isActive():
            self._overlay_timer.start(0)

    def _sync_chat_overlays(self):
        if hasattr(self, 'welcome_area'):
            welcome = not self.welcome_widget.isHidden()
            self.welcome_area.setVisible(welcome)
        self._position_attachments_panel()
        self._position_error_panel()
        self._position_tags_bar()
        if hasattr(self, "context_offer"):
            self.context_offer.position()
        self._position_timeline_rail()
        if hasattr(self,'chat_references'):self.chat_references.update_search_visibility()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._schedule_overlay_update()

    @staticmethod
    def _tag_instruction(tags: List[str]) -> str:
        clean_tags = [str(tag).strip() for tag in tags if str(tag).strip()]
        if not clean_tags:
            return ""
        return f"Instructions for this request only: {', '.join(clean_tags)}"

    @classmethod
    def _build_tagged_prompt(cls, text: str, tags: List[str]) -> str:
        instruction = cls._tag_instruction(tags)
        return f"{text.rstrip()}\n\n{instruction}" if instruction else text.rstrip()

    @staticmethod
    def _build_canvas_context_prompt(text: str, canvas_context: Dict[str, Any]) -> str:
        """Serialize Board context for the model while keeping machine paths out of Chat UI."""
        from core.canvas_context import model_canvas_context
        model_context = model_canvas_context(canvas_context)

        compact_json = json.dumps(
            model_context,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        explanation = (
            "Read the supplied nodes and directed edges as context for the user's request. "
            "Edges point from fromNode to toNode. explicitNodes identifies the user's selection; "
            "other supplied nodes are downstream context. This is a read-only snapshot."
        )
        context_block = f"{explanation}\n<canvas_context>{compact_json}</canvas_context>"
        user_text = str(text or "").strip()
        return f"{context_block}\n\n{user_text}" if user_text else context_block

    @staticmethod
    def _build_file_context_prompt(text: str, file_context: Dict[str, Any]) -> str:
        """Add the exact machine path for file tools without exposing it in Chat UI."""
        context = dict(file_context or {})
        if not context.get("includePath"):
            return str(text or "").strip()
        model_path = str(context.get("modelPath") or "").strip()
        if not model_path:
            return str(text or "").strip()
        instruction = (
            "The current file's exact filesystem path is shown below. Use this exact path "
            "when calling file tools; the friendlier path visible in the user's message is "
            "only for display.\n"
            f"<file_path>{model_path}</file_path>"
        )
        user_text = str(text or "").strip()
        return f"{instruction}\n\n{user_text}" if user_text else instruction

    @classmethod
    def _strip_tag_instruction_for_display(cls, content: str, tags: List[str]) -> str:
        clean = content or ""
        instruction = cls._tag_instruction(tags)
        if instruction and clean.rstrip().endswith(instruction):
            return clean.rstrip()[:-len(instruction)].rstrip()
        # Legacy format from older sessions: lower-cased tag words with trailing periods.
        if tags:
            legacy = " ".join([t.strip().lower() + ("" if t.strip().endswith(".") else ".") for t in tags])
            if clean.rstrip().endswith(legacy):
                return clean.rstrip()[:-len(legacy)].rstrip()
        return clean

    def _on_user_scroll_started(self):
        # Suspend before Qt handles the wheel/key. A queued layout/range update
        # must not pull the viewport back down during the user's movement.
        self._auto_scroll_to_bottom = False
        if self.scroll_area.middle_scroll.active:
            self._auto_scroll_to_bottom = False
        self._scroll_navigation_revision = getattr(self, '_scroll_navigation_revision', 0) + 1
        self._cancel_history_positioning()
        self._user_scroll_pending = True
        self._user_scroll_timer.start(0)

    def _finish_user_scroll(self):
        self._user_scroll_pending = False
        if self._history_fetching or self.scroll_area.middle_scroll.active:
            self._auto_scroll_to_bottom = False
            return
        if self._thought_scroll_anchor is None:
            bar = self.scroll_area.verticalScrollBar()
            self._auto_scroll_to_bottom = bar.value() == bar.maximum()

    def _on_scroll_value_changed(self, val: int):
        if self.scroll_area.middle_scroll.active:
            self._auto_scroll_to_bottom = False
            return
        if self._thought_scroll_anchor is not None or self._history_positioning:
            return
        bar = self.scroll_area.verticalScrollBar()
        # Изменение диапазона/layout не означает, что пользователь ушёл вверх.
        if self._user_scroll_pending or bar.isSliderDown():
            self._auto_scroll_to_bottom = val == bar.maximum()

    def _can_follow_latest_content(self):
        return (self._auto_scroll_to_bottom and not self._user_scroll_pending
                and self._thought_scroll_anchor is None and not self._history_prepending
                and not self._history_positioning and not self.scroll_area.middle_scroll.active)

    def _follow_latest_content(self):
        if self._can_follow_latest_content():
            bar = self.scroll_area.verticalScrollBar()
            bar.setValue(bar.maximum())

    def _on_scroll_range_changed(self, min_val: int, max_val: int):
        if self._thought_scroll_anchor is not None:
            self._restore_thought_position()
        elif self._history_positioning:
            self.scroll_area.verticalScrollBar().setValue(max_val)
        elif self._auto_scroll_to_bottom and not self._user_scroll_pending:
            # Each newly wrapped line changes the range. Follow it in this
            # layout pass, without interpolation or a deferred camera timer.
            self._follow_latest_content()

    def _hold_thought_position(self, turn):
        self._thought_scroll_anchor = (turn, turn.mapTo(self.scroll_area.viewport(), QPoint(0, 0)).y())
        self._thought_anchor_passes = 0
        self._auto_scroll_to_bottom = False
        self._thought_anchor_timer.start(0)

    def _restore_thought_position(self):
        from shiboken6 import isValid
        if self._thought_scroll_anchor is None:
            return
        turn, offset = self._thought_scroll_anchor
        if not isValid(turn):
            self._thought_scroll_anchor = None
            return
        current = turn.mapTo(self.scroll_area.viewport(), QPoint(0, 0)).y()
        bar = self.scroll_area.verticalScrollBar()
        if current != offset:
            bar.setValue(bar.value() + current - offset)

    def _settle_thought_position(self):
        self.turns_layout.activate()
        self._restore_thought_position()
        self._thought_anchor_passes += 1
        if self._thought_scroll_anchor is not None and self._thought_anchor_passes < 2:
            self._thought_anchor_timer.start(16)
        else:
            self._thought_scroll_anchor = None
            bar = self.scroll_area.verticalScrollBar()
            self._auto_scroll_to_bottom = bar.value() == bar.maximum()

    def reset_to_new_chat(self, folder_id=None):
        self.input_state.stash()
        self._history_scroll_timer.stop()
        self._history_positioning = False
        self._stash_session()
        self._history_before = 0
        self._history_anchor = None
        self._history_fetching = False
        self._thought_scroll_anchor = None
        self._thought_anchor_timer.stop()
        self._session_load_generation += 1
        self._session_load_guard["generation"] = self._session_load_generation
        self._session_render_timer.stop()
        self._session_render_messages = None
        self._set_session_loading(False)
        self._session_sync_retry_timer.stop()
        self._pending_retry = None
        self._pending_assistant_identity = None
        self._pending_assistant_prefix = ""
        if hasattr(self, 'chat_references'):
            self.chat_references.reset()
        if hasattr(self, 'message_selection'):
            self.message_selection.cancel(force=True)
        self._clear_error_panel()
        self._pending_new_session_folder_id = str(folder_id) if folder_id else None
        self._delete_temporary_attachment_sources(self._pending_attachments)
        self._pending_attachments.clear()
        if hasattr(self, "attachments_panel"):
            self.attachments_panel.set_items([])
        self._set_attachment_preparing(False)
        old_key = self._current_run_key()
        if old_key and self.current_worker and self.current_worker.isRunning():
            self._detached_runs.add(old_key)
        app_state.select_session(None, "main")
        self.current_session_id = None
        self.model_selector.set_profile('')
        for dialog in self._pipeline_dialogs.values():
            dialog.hide()
        self.current_branch = "main"
        self.input_state.reset()
        self.reasoning_selector.restore('')
        self.current_temp_memory_mode = "text"
        self.current_include_thoughts_in_context = True
        self.welcome_widget.reset_defaults()
        self.messages_history.clear()
        self.user_turn_widgets.clear()
        self.timeline_rail.update_turns([])
        self.timeline_rail.set_history_markers([])
        self._history_jump_target = None

        while self.turns_layout.count() > 1:
            w = self.turns_layout.takeAt(0).widget()
            if w:
                w.hide()
                w.deleteLater()

        self.scroll_area.hide()
        self.welcome_widget.show()
        self.current_worker = None
        self.active_thought_turn = None
        self.active_assistant_turn = None
        self._generation_insert_index = None
        self._retry_mode_active = False
        self._set_send_button_running(False)

    def _next_generated_turn_index(self, insert_index=None):
        if insert_index is not None:
            return insert_index
        index = self._generation_insert_index
        if index is None:
            index = self.turns_layout.count() - 1
        run_id = self.current_worker.run_id if self.current_worker else None
        # Хвост старого потока вставляется перед ещё не применёнными уточнениями.
        if run_id:
            for candidate in range(index):
                turn = self.turns_layout.itemAt(candidate).widget()
                if (getattr(turn, '_steering_waiting', False)
                        and getattr(turn, '_steering_run_id', None) == run_id):
                    index = candidate
                    break
        if self._generation_insert_index is not None:
            self._generation_insert_index += 1
        return index

    def _create_thought_turn(self, insert_index: Optional[int] = None, *, defer_body=False) -> ThoughtMessageTurn:
        turn = ThoughtMessageTurn(parent=self.turns_container, defer_body=defer_body)
        from ui.views.chat_resume import install_controls
        install_controls(self, turn)
        if self.current_worker:
            turn._run_state = {'run_id': self.current_worker.run_id, 'status': 'running', 'stage': 'thought'}
        from ui.views.chat_model_blocks import inherit_resume_identity
        inherit_resume_identity(self, turn)
        turn.delete_requested.connect(self._on_turn_delete_requested)
        turn.create_branch_requested.connect(self._on_create_branch_from_turn)
        turn.content_edited.connect(self._sync_session_to_disk)
        index = self._next_generated_turn_index(insert_index)
        self.turns_layout.insertWidget(index, turn)
        self.turns_layout.activate()
        turn.ensurePolished()
        turn.layout().activate()
        return turn

    def _create_assistant_turn(
        self,
        segment_kind: str = "final",
        insert_index: Optional[int] = None,
    ) -> AssistantMessageTurn:
        turn = AssistantMessageTurn(parent=self.turns_container, segment_kind=segment_kind)
        from ui.views.chat_resume import install_controls
        install_controls(self, turn)
        if self.current_worker:
            turn._run_state = {'run_id': self.current_worker.run_id, 'status': 'running', 'stage': 'answer'}
        from ui.views.chat_model_blocks import inherit_resume_identity
        inherit_resume_identity(self, turn)
        turn.stamp_current_time()
        turn.delete_requested.connect(self._on_turn_delete_requested)
        turn.create_branch_requested.connect(self._on_create_branch_from_turn)
        turn.content_edited.connect(self._sync_session_to_disk)
        index = self._next_generated_turn_index(insert_index)
        self.turns_layout.insertWidget(index, turn)
        self.turns_layout.activate()
        turn.ensurePolished()
        turn.layout().activate()
        return turn

    def _create_subagent_turn(self, results=None, insert_index=None):
        turn = SubagentMessageTurn(self.turns_container)
        turn.delete_requested.connect(self._on_turn_delete_requested)
        turn.create_branch_requested.connect(self._on_create_branch_from_turn)
        from ui.views.chat_model_blocks import inherit_resume_identity
        inherit_resume_identity(self, turn)
        turn.set_results(results or [])
        self.turns_layout.insertWidget(self._next_generated_turn_index(insert_index), turn)
        return turn

    def _on_subagent_event(self, payload):
        run_id = self.current_worker.run_id if self.current_worker else ''
        turn = next((self.turns_layout.itemAt(i).widget() for i in range(self.turns_layout.count())
            if isinstance(self.turns_layout.itemAt(i).widget(), SubagentMessageTurn)
            and (getattr(self.turns_layout.itemAt(i).widget(), '_run_state', None) or {}).get('run_id') == run_id), None)
        if turn is None:
            index = self.turns_layout.indexOf(self.active_assistant_turn) if self.active_assistant_turn else -1
            turn = self._create_subagent_turn(insert_index=index if index >= 0 else None)
            turn._run_state = {'run_id':run_id, 'status':'running'}
        turn.update_result(payload)
        if payload.get('chat_id'):turn.chat_meta['chat_id'] = payload['chat_id']

    def _render_saved_run_segments(self, segments: List[Dict[str, Any]]) -> bool:
        current_thought: Optional[ThoughtMessageTurn] = None
        current_phase_seconds = 0.0
        rendered = False

        def ensure_thought() -> ThoughtMessageTurn:
            nonlocal current_thought
            if current_thought is None:
                current_thought = self._create_thought_turn(defer_body=True)
            return current_thought

        def finish_thought() -> None:
            nonlocal current_thought, current_phase_seconds, rendered
            if current_thought is None:
                return
            try:
                current_thought.auto_collapse(
                    elapsed_seconds=current_phase_seconds if current_phase_seconds > 0 else 0.0
                )
                if getattr(current_thought, '_phase_timing_unknown', False):
                    if current_thought.transcript_segments:
                        current_thought.transcript_segments[0]['phase_timing_unknown'] = True
                    suffix = tr('ui.caption.939e30e5e5d3', v0=current_thought.tools_count) if current_thought.tools_count else ''
                    bind_caption(current_thought.meta_badge, tr('ui.caption.0b04c334c7ca') + suffix)
            except RuntimeError:
                pass
            rendered = True
            current_thought = None
            current_phase_seconds = 0.0

        for raw_segment in segments or []:
            if not isinstance(raw_segment, dict):
                continue
            segment = dict(raw_segment)
            kind = str(segment.get("type") or "")
            try:
                current_phase_seconds += max(0.0, float(segment.get("phase_seconds") or 0.0))
            except (TypeError, ValueError):
                pass

            if kind == 'user_update':
                finish_thought()
                from ui.views.chat_steering import render_update
                render_update(self, segment, segment.get('run_id', ''))
                rendered = True
                continue

            if segment.get('phase_timing_unknown') and kind in {'reasoning', 'tool'}:
                ensure_thought()._phase_timing_unknown = True

            if kind == "reasoning":
                text = str(segment.get("text") or "")
                if text:
                    thought = ensure_thought()
                    from ui.views.chat_references import restore_turn
                    restore_turn(thought, segment)
                    thought.append_thought(text)
                    thought.transcript_segments[-1]['ui_sources'] = segment.get('ui_sources') or []
                    thought._refresh_sources()
                continue

            if kind == "tool":
                thought = ensure_thought()
                from ui.views.chat_references import restore_turn
                restore_turn(thought, segment)
                tool_id = str(segment.get("id") or "")
                tool_name = str(segment.get("name") or "tool")
                args = segment.get("args") if isinstance(segment.get("args"), dict) else {}
                thought.add_tool_start(tool_name, args, tool_id)
                output = str(segment.get("output") or "")
                if output:
                    thought.add_tool_result(tool_id, tool_name, output, segment.get('status', 'completed'))
                thought.add_tool_sources(tool_id, segment.get('ui_sources') or [])
                continue

            if kind == 'pipeline_notification':
                render_notification(self, segment)
                rendered = True
                continue

            if kind == 'subagents':
                finish_thought()
                turn = self._create_subagent_turn(segment.get('results') or [])
                from ui.views.chat_references import restore_turn
                restore_turn(turn, segment)
                rendered = True
                continue

            if kind in {"assistant_interim", "assistant_final"}:
                text = str(segment.get("text") or "")
                if not text.strip():
                    continue
                finish_thought()
                turn = self._create_assistant_turn(
                    segment_kind="interim" if kind == "assistant_interim" else "final"
                )
                turn.append_content(text)
                from ui.views.chat_references import restore_turn
                restore_turn(turn, segment)
                rendered = True

        finish_thought()
        return rendered

    def _set_session_loading(self, loading):
        self._session_loading = loading
        # Keep partially restored history inaccessible without cascading
        # EnabledChange/style events through thousands of message children.
        self.turns_container.setAttribute(Qt.WA_TransparentForMouseEvents, loading)
        self.turns_layout.setEnabled(not loading)
        self.turns_container.setUpdatesEnabled(not loading)
        self.tags_bar.setEnabled(not loading)
        self.pipeline_selector.setEnabled(not loading)
        self.model_selector.setEnabled(not loading and not self._model_change_pending)
        self.timeline_rail.setEnabled(not loading)
        self.message_selection.gutter.setEnabled(not loading)
        self._set_attachment_preparing(self._preparing_attachments)

    def _begin_session_load(self, session_id, branch):
        self.reset_to_new_chat()
        self.current_session_id = session_id
        self.current_branch = branch or "main"
        # Detach any target run before the event loop starts delivering chunks.
        run_key = self._current_run_key()
        if run_key in self._active_runs:
            self._detached_runs.add(run_key)
        self._set_session_loading(True)
        app_state.select_session(session_id, self.current_branch)
        self._auto_scroll_to_bottom = True
        self._history_positioning = True
        self.welcome_widget.hide()
        self.scroll_area.show()
        return self._session_load_generation

    def _session_load_failed(self, error):
        self.reset_to_new_chat()
        self.welcome_widget.hide()
        self.scroll_area.show()
        self._show_error_panel(tr('ui.caption.43232efe2cad', v0=error))

    def open_session(self, session_id: str, branch: str = "main", *, force=False):
        """Sidebar navigation: fetch off-thread and build history in short batches."""
        if not force and (session_id, branch) == (self.current_session_id, self.current_branch) and not self._session_loading:
            self._position_history_at_end()
            return
        retained_before = None
        if force:
            existing = self._session_cache.get((session_id, branch))
            retained_before = existing['before'] if existing else None
            if (session_id, branch) == (self.current_session_id, self.current_branch):
                retained_before = self._history_before
            self._drop_cached_session((session_id, branch))
        cached = self._session_cache.pop((session_id, branch), None)
        if cached and cached.get('stale'):
            retained_before = cached['before']
            for widget in cached['widgets']:
                widget.deleteLater()
            cached = None
        generation = self._begin_session_load(session_id, branch)
        if force:
            self._drop_cached_session((session_id, branch))
        if cached:
            self._restore_cached_session(cached)
            return
        manager = self.session_manager
        guard = self._session_load_guard

        def fetch():
            # Requests still queued after another click need no network work.
            # The worker reads plain Python data, never widgets or Qt state.
            if guard["generation"] != generation:
                return generation, None, None
            try:
                return generation, self._fetch_session_for_ui(session_id, branch, retained_before), None
            except Exception as error:
                return generation, None, error

        self.session_load_runner.submit(fetch, self._session_load_received)

    def _session_load_received(self, result):
        generation, payload, error = result
        if generation != self._session_load_generation:
            return
        if error is not None:
            self._session_load_failed(error)
            return
        try:
            if isinstance(payload, dict):
                raw_messages, metadata, active_b = payload['messages'], payload['metadata'], payload['branch']
                self._history_before = payload.get('page', {}).get('before', 0)
                self.timeline_rail.set_history_markers(payload.get('timeline', []))
                self._history_anchor = raw_messages[0].additional_kwargs.get('chat_id') if raw_messages else None
            else:
                raw_messages, metadata, active_b = payload
            self._apply_session_metadata(self.current_session_id, metadata, active_b)
            if generation != self._session_load_generation:
                return
            self._session_render_messages = iter(raw_messages)
            self._session_render_timer.start()
        except Exception as error:
            self._session_load_failed(error)

    def _render_session_batch(self):
        if not self._session_loading or self._session_render_messages is None:
            return
        deadline = time.perf_counter() + 0.010
        try:
            while True:
                try:
                    message = next(self._session_render_messages)
                except StopIteration:
                    self._session_render_messages = None
                    self._finish_loaded_session()
                    return
                self._render_saved_message(message)
                if time.perf_counter() >= deadline:
                    self._session_render_timer.start()
                    return
        except Exception as error:
            self._session_load_failed(error)

    def load_session(self, session_id: str, branch: str = "main"):
        """Complete synchronously for integrations that send immediately afterward.

        Interactive session selection uses open_session instead. Keeping this
        contract avoids starting an editor/board/task request with partial history.
        """
        retained_before = self._history_before if (session_id, branch) == (self.current_session_id, self.current_branch) else None
        self._begin_session_load(session_id, branch)
        self._drop_cached_session((session_id, branch))
        try:
            payload = self._fetch_session_for_ui(session_id, branch, retained_before)
            if isinstance(payload, dict):
                raw_messages, metadata, active_b = payload['messages'], payload['metadata'], payload['branch']
                self._history_before = payload.get('page', {}).get('before', 0)
                self.timeline_rail.set_history_markers(payload.get('timeline', []))
                self._history_anchor = raw_messages[0].additional_kwargs.get('chat_id') if raw_messages else None
            else:
                raw_messages, metadata, active_b = payload
            self._apply_session_metadata(session_id, metadata, active_b)
            for message in raw_messages:
                self._render_saved_message(message)
            self._finish_loaded_session()
        except Exception as error:
            self._session_load_failed(error)

    def _apply_session_metadata(self, session_id, metadata, active_b):
        self._session_metadata = dict(metadata)
        self.current_branch = active_b
        self.model_selector.set_profile(metadata.get("model_profile_id", ""))
        self.reasoning_selector.restore(metadata.get("reasoning_effort", ""))
        effective = metadata.get('effective_project_settings') or {}
        if effective.get('project_id'):
            app_state.session_projects[session_id] = effective['project_id']
            app_state.select_project(effective['project_id'], effective.get('project_name'),
                                     effective.get('workspace_path') or app_state._default_workspace)
        self.current_workspace_root = app_state.workspace
        app_state.select_session(
            session_id,
            active_b,
            workspace=self.current_workspace_root,
        )
        self.current_temp_memory_mode = metadata.get("temp_memory_mode", "text")
        self.current_include_thoughts_in_context = bool(
            metadata.get("include_thoughts_in_context", True)
        )

        self.input_state.restore(session_id, active_b, metadata)
        self._schedule_overlay_update()

    def _render_saved_message(self, msg):
        insertion_index = self._generation_insert_index
        rendering_worker = self.current_worker
        resume_target = getattr(self, '_resume_target', None)
        # Saved cards must never inherit ownership or grouping from the live
        # generation. Otherwise folding that run can delete old assistant cards.
        self._generation_insert_index = None
        self.current_worker = None
        self._resume_target = None
        first_card = self.turns_layout.count() - 1
        try:
            role = "user" if msg.type == "human" else "assistant"
            content = msg.content or ""
            history_item = {"role": role, "content": content}
            if getattr(msg, "additional_kwargs", None):
                history_item["additional_kwargs"] = dict(msg.additional_kwargs)
            self.messages_history.append(history_item)

            if role == "user":
                # Извлекаем теги из additional_kwargs (или из старого формата <!--tags:...--> для совместимости)
                user_kwargs = dict(getattr(msg, "additional_kwargs", {}) or {})
                extracted_tags = user_kwargs.get("tags", [])
                extracted_canvas_context = dict(user_kwargs.get("canvas_context") or {})
                saved_canvas_user_text = user_kwargs.get("canvas_user_text")
                extracted_file_context = dict(user_kwargs.get("file_context") or {})
                saved_file_user_text = user_kwargs.get("file_user_text")
                if extracted_canvas_context and saved_canvas_user_text is not None:
                    clean_text = str(saved_canvas_user_text or "")
                elif extracted_file_context and saved_file_user_text is not None:
                    clean_text = str(saved_file_user_text or "")
                else:
                    clean_text = content
                tag_match = None

                if not extracted_tags and not extracted_canvas_context and not extracted_file_context:
                    tag_match = re.search(r"<!--tags:(.*?)-->", content)
                    if tag_match:
                        extracted_tags = [t.strip() for t in tag_match.group(1).split("|") if t.strip()]
                        clean_text = content[:tag_match.start()].rstrip()

                if not extracted_tags and not extracted_canvas_context and not extracted_file_context:
                    instruction_match = re.search(r"(?:^|\n)Instructions for this request only:\s*(.+?)\s*$", content)
                    if instruction_match:
                        extracted_tags = [t.strip() for t in instruction_match.group(1).split(",") if t.strip()]
                        clean_text = content[:instruction_match.start()].rstrip()

                # Инструкция тегов хранится в самом сообщении для стабильности истории/кэша,
                # но в карточке пользователя показывается только исходный вопрос.
                if extracted_tags and not tag_match and not extracted_canvas_context and not extracted_file_context:
                    clean_text = self._strip_tag_instruction_for_display(clean_text, extracted_tags)

                extracted_attachments = list(user_kwargs.get("attachments") or [])
                turn = UserMessageTurn(
                    text=clean_text,
                    display_text=user_kwargs.get("task_status_display") if user_kwargs.get("task_status_check") else None,
                    tags=extracted_tags,
                    attachments=extracted_attachments,
                    canvas_context=extracted_canvas_context,
                    file_context=extracted_file_context,
                    attachment_resolver=self._resolve_attachment_for_ui,
                    parent=self.turns_container,
                )
                turn.delete_requested.connect(self._on_turn_delete_requested)
                turn.retry_requested.connect(self._on_turn_retry_requested)
                turn.create_branch_requested.connect(self._on_create_branch_from_turn)
                turn.content_edited.connect(self._sync_session_to_disk)
                self.turns_layout.insertWidget(self.turns_layout.count() - 1, turn)
                self.user_turn_widgets.append(turn)
                from ui.views.chat_references import restore_turn
                restore_turn(turn, user_kwargs)
            else:
                think_match = re.search(r"<think>([\s\S]*?)</think>", content)
                clean_answer = content
                assistant_kwargs = dict(getattr(msg, "additional_kwargs", {}) or {})
                from core.chat_runs import repair_steered_projection
                assistant_kwargs = repair_steered_projection(assistant_kwargs, content)
                saved_run_segments = assistant_kwargs.get("run_segments")
                if isinstance(saved_run_segments, list) and self._render_saved_run_segments(saved_run_segments):
                    from ui.views.chat_resume import restore_controls
                    restore_controls(self, assistant_kwargs, first_card)
                    return

                saved_thinking_seconds = assistant_kwargs.get("thinking_seconds")
                saved_thinking_had_activity = bool(assistant_kwargs.get("thinking_had_activity", False))
                try:
                    saved_tools_count = max(0, int(assistant_kwargs.get("thinking_tools_count", 0) or 0))
                except (TypeError, ValueError):
                    saved_tools_count = 0

                if think_match:
                    thought_body = think_match.group(1).strip()
                    clean_answer = re.sub(r"<think>[\s\S]*?</think>", "", content).strip()
                    saved_thinking_had_activity = True
                else:
                    thought_body = ""

                if think_match or saved_thinking_had_activity:
                    t_turn = ThoughtMessageTurn(parent=self.turns_container, defer_body=True)
                    if thought_body:
                        t_turn.append_thought(thought_body)
                    t_turn.tools_count = saved_tools_count
                    t_turn.auto_collapse(elapsed_seconds=saved_thinking_seconds)
                    t_turn.delete_requested.connect(self._on_turn_delete_requested)
                    t_turn.create_branch_requested.connect(self._on_create_branch_from_turn)
                    t_turn.content_edited.connect(self._sync_session_to_disk)
                    self.turns_layout.insertWidget(self.turns_layout.count() - 1, t_turn)
                    from ui.views.chat_references import restore_turn
                    restore_turn(t_turn, assistant_kwargs.get('chat_thought') or {})

                if not clean_answer.strip():
                    return
                a_turn = AssistantMessageTurn(parent=self.turns_container)
                a_turn.stamp_current_time()
                a_turn.delete_requested.connect(self._on_turn_delete_requested)
                a_turn.create_branch_requested.connect(self._on_create_branch_from_turn)
                a_turn.content_edited.connect(self._sync_session_to_disk)
                a_turn.append_content(clean_answer)
                self.turns_layout.insertWidget(self.turns_layout.count() - 1, a_turn)
                from ui.views.chat_references import restore_turn
                restore_turn(a_turn, assistant_kwargs)
                from ui.views.chat_resume import restore_controls
                restore_controls(self, assistant_kwargs, first_card)

        finally:
            self._generation_insert_index = insertion_index
            self.current_worker = rendering_worker
            self._resume_target = resume_target
            if msg.type != "human":
                identity = object()
                for index in range(first_card, self.turns_layout.count() - 1):
                    turn = self.turns_layout.itemAt(index).widget()
                    if isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn)):
                        turn._display_group_id = identity
            self.message_selection._turns_key = None
            self.message_selection.turns()

    def _finish_loaded_session(self):
        self._set_session_loading(False)
        self.message_selection.schedule_layout_sync()
        self.timeline_rail.update_turns(self.user_turn_widgets)
        self.welcome_widget.hide()
        self.scroll_area.show()

        run_key = self._current_run_key()
        self.chat_references.refresh()
        from ui.views.chat_resume import refresh_controls
        refresh_controls(self)
        self.current_worker = self._active_runs.get(run_key)
        if self.current_worker and self.current_worker.isRunning():
            self._detached_runs.add(run_key)
            self._add_system_message(tr('ui.caption.2f073ae0fe0d'))
            self._set_send_button_running(True)
        self._position_history_at_end()

    def _snapshot_turn_messages(self, turns=None, *, include_prefix=False):
        """Serialize the given visible turns, preserving only their segments."""
        from ui.views.chat_references import turn_metadata
        from core.chat_references import ensure_history_ids
        reconstructed_messages = []
        new_history = []
        pending_thought = ""
        run_segments: List[Dict[str, Any]] = []
        run_thinking_seconds = 0.0
        run_thinking_tools_count = 0
        run_state = None

        def reset_run_state():
            nonlocal pending_thought, run_segments, run_thinking_seconds, run_thinking_tools_count, run_state
            pending_thought = ""
            run_segments = []
            run_thinking_seconds = 0.0
            run_thinking_tools_count = 0
            run_state = None

        def flush_incomplete_run():
            nonlocal run_segments
            if not run_segments:
                return
            visible_indexes = [
                index for index, segment in enumerate(run_segments)
                if segment.get("type") in {"assistant_interim", "assistant_final"}
                and str(segment.get("text") or "").strip()
            ]
            if visible_indexes:
                last_index = visible_indexes[-1]
                final_text = str(run_segments[last_index].get("text") or "")
                run_segments[last_index]["type"] = "assistant_final"
            else:
                final_text = '' if any(s.get('type') == 'subagents' for s in run_segments) else '\n\n'.join(
                    tr('ui.caption.d165cb7a7c0e') + str(seg.get('name') or 'tool') + '\n' + str(seg.get('output') or '')
                    for seg in run_segments if seg.get('type') == 'tool')
            reasoning = '\n'.join(str(seg.get('text') or '') for seg in run_segments if seg.get('type') == 'reasoning')
            if reasoning:
                final_text = '<think>' + reasoning + '</think>\n' + final_text
            assistant_kwargs = {
                "thinking_seconds": round(run_thinking_seconds, 3),
                "thinking_had_activity": bool(
                    any(seg.get("type") in {"reasoning", "tool"} for seg in run_segments)
                ),
                "thinking_tools_count": run_thinking_tools_count,
                "run_segments": [dict(segment) for segment in run_segments],
            }
            if run_state:
                assistant_kwargs['run_state'] = dict(run_state)
            if visible_indexes:
                assistant_kwargs.update({key: run_segments[last_index][key] for key in ('chat_id', 'chat_pinned')
                                         if key in run_segments[last_index]})
            reconstructed_messages.append(AIMessage(content=final_text, additional_kwargs=assistant_kwargs))
            new_history.append({
                "role": "assistant",
                "content": final_text,
                "additional_kwargs": assistant_kwargs,
            })
            reset_run_state()

        if turns is None:
            turns = [self.turns_layout.itemAt(i).widget() for i in range(self.turns_layout.count())]
        for w in turns:
            if isinstance(w, UserMessageTurn):
                if getattr(w, '_steering_message', None):
                    # An unlocked update uses the same editable UI projection
                    # as ordinary user messages; retain its existing run envelope.
                    message = dict(w._steering_message)
                    data = dict(message.get('data') or {})
                    metadata = {**dict(data.get('additional_kwargs') or {}), **turn_metadata(w)}
                    metadata['tags'] = list(w.tags)
                    metadata['attachments'] = [dict(item) for item in w.attachments]
                    data.update(content=w.raw_text, additional_kwargs=metadata)
                    message['data'] = data
                    run_segments.append({'type': 'user_update', 'message': message,
                        'chat_id': w.chat_meta.get('chat_id'), 'run_id': w._steering_run_id, 'applied': True})
                    continue
                # A new user message starts a new logical turn. Any complete previous
                # assistant turn has already been flushed when its final segment appeared.
                if run_segments:
                    flush_incomplete_run()

                msg_kwargs = turn_metadata(w)
                if getattr(w, "tags", None):
                    msg_kwargs["tags"] = list(w.tags)
                if getattr(w, "attachments", None):
                    msg_kwargs["attachments"] = [dict(item) for item in w.attachments]
                if getattr(w, "canvas_context", None):
                    msg_kwargs["canvas_context"] = dict(w.canvas_context)
                    msg_kwargs["canvas_user_text"] = w.raw_text
                    base_user_msg = self._build_canvas_context_prompt(w.raw_text, w.canvas_context)
                elif getattr(w, "file_context", None):
                    msg_kwargs["file_context"] = dict(w.file_context)
                    msg_kwargs["file_user_text"] = w.raw_text
                    base_user_msg = self._build_file_context_prompt(w.raw_text, w.file_context)
                else:
                    base_user_msg = w.raw_text
                full_user_msg = self._build_tagged_prompt(base_user_msg, w.tags) if w.tags else base_user_msg
                reconstructed_messages.append(HumanMessage(content=full_user_msg, additional_kwargs=msg_kwargs))
                history_item = {"role": "user", "content": full_user_msg}
                if msg_kwargs:
                    history_item["additional_kwargs"] = msg_kwargs
                new_history.append(history_item)

            elif isinstance(w, NotificationTurn):
                run_state = getattr(w, '_run_state', None) or run_state
                run_segments.extend(w.export_segments())

            elif isinstance(w, SubagentMessageTurn):
                run_state = getattr(w, '_run_state', None) or run_state
                run_segments.extend({**segment, **turn_metadata(w)} for segment in w.export_segments())

            elif isinstance(w, ThoughtMessageTurn):
                run_state = getattr(w, '_run_state', None) or run_state
                pending_thought = w.total_thought_text.strip()
                run_thinking_seconds += max(0.0, float(w.elapsed_seconds))
                run_thinking_tools_count += max(0, int(w.tools_count))
                exported = w.export_segments()
                if exported:
                    run_segments.extend({**segment, **turn_metadata(w)} for segment in exported)

            elif isinstance(w, AssistantMessageTurn):
                run_state = getattr(w, '_run_state', None) or run_state
                if not w.raw_content.strip():
                    continue
                kind = str(getattr(w, "segment_kind", "final") or "final")
                is_interim = kind == "interim"
                if w.raw_content.strip():
                    run_segments.append({
                        "type": "assistant_interim" if is_interim else "assistant_final",
                        "text": w.raw_content,
                        **turn_metadata(w),
                    })

                if is_interim:
                    pending_thought = ""
                    continue

                reasoning = '\n'.join(str(seg.get('text') or '') for seg in run_segments if seg.get('type') == 'reasoning')
                full_content = f'<think>{reasoning}</think>\n{w.raw_content}' if reasoning else w.raw_content

                assistant_kwargs = {
                    "thinking_seconds": round(run_thinking_seconds, 3),
                    "thinking_had_activity": bool(
                        any(seg.get("type") in {"reasoning", "tool"} for seg in run_segments)
                    ),
                    "thinking_tools_count": run_thinking_tools_count,
                    **turn_metadata(w),
                }
                if run_segments:
                    assistant_kwargs["run_segments"] = [dict(segment) for segment in run_segments]
                if run_state:
                    assistant_kwargs['run_state'] = dict(run_state)

                reconstructed_messages.append(AIMessage(content=full_content, additional_kwargs=assistant_kwargs))
                history_item = {"role": "assistant", "content": full_content}
                if assistant_kwargs:
                    history_item["additional_kwargs"] = assistant_kwargs
                new_history.append(history_item)
                reset_run_state()

        if run_segments:
            flush_incomplete_run()

        ensure_history_ids(reconstructed_messages, self.current_session_id or '', start_index=self._history_before)
        for message, item in zip(reconstructed_messages, new_history):
            item['additional_kwargs'] = dict(message.additional_kwargs)
        if include_prefix:
            prefix = self._history_prefix()
            prefix_history = [{'role': 'user' if m.type == 'human' else 'assistant', 'content': m.content,
                'additional_kwargs': dict(m.additional_kwargs)} for m in prefix if m.type in {'human', 'ai'}]
            return prefix + reconstructed_messages, prefix_history + new_history
        return reconstructed_messages, new_history

    def _sync_session_to_disk(self, on_saved=None, on_failed=None):
        """Пересобирает и сохраняет актуальные сообщения и теги через ядро."""
        if not self.current_session_id or self._session_loading or self._history_prepending:
            return
        self.chat_references.refresh()
        reconstructed_messages, new_history = self._snapshot_turn_messages()
        self.messages_history = new_history

        session_id = self.current_session_id
        branch = self.current_branch
        active_tags = list(self.tags_bar.get_active_tags())
        prefix_count, prefix_anchor = self._history_before, self._history_anchor

        def save_snapshot():
            self.session_manager.replace_messages(
                session_id=session_id,
                messages=reconstructed_messages,
                branch=branch,
                preserve_prefix=prefix_count,
                prefix_anchor=prefix_anchor,
            )
            self.session_manager.update_metadata(
                session_id,
                {"active_tags": active_tags},
                branch=branch,
            )
            # Removing an attachment from every branch/message makes a copied
            # session file orphaned. Cleanup is owned by the core as well.
            self.session_manager.cleanup_attachments(session_id)

        def save_failed(error):
            if on_failed is not None:
                on_failed(error)
            text = str(error or "").lower()
            connection_problem = (
                "connection refused" in text
                or "connecterror" in text
                or "all connection attempts failed" in text
                or "errno 111" in text
                or not core_process.is_running()
            )
            if connection_problem:
                # Session files are core-owned. Keep the edited UI state and
                # silently retry once the core becomes reachable instead of
                # flashing a scary banner for a transient restart/offline gap.
                if self.current_session_id == session_id:
                    self._session_sync_retry_timer.start()
                return
            self._show_error_panel(tr('ui.caption.132d41b8b5c7', v0=error))

        def save_succeeded(_result):
            anchor = reconstructed_messages[0].additional_kwargs.get('chat_id') if reconstructed_messages else None
            if (self.current_session_id, self.current_branch) == (session_id, branch) and self._history_before == prefix_count:
                self._history_anchor = anchor
            cached = self._session_cache.get((session_id, branch))
            if cached and cached['before'] == prefix_count:
                cached['anchor'] = anchor
            if self._session_sync_retry_timer.isActive():
                self._session_sync_retry_timer.stop()
            event_bus.sessions_updated.emit()
            if on_saved is not None:
                on_saved()

        if not core_process.is_running():
            self._session_sync_retry_timer.start()
            if on_failed is not None:
                on_failed(ConnectionError(tr('ui.caption.b019a82ca6db')))
            return

        self.session_write_runner.submit(
            save_snapshot,
            on_success=save_succeeded,
            on_error=save_failed,
        )

    def _choose_attachments(self):
        if self._preparing_attachments:
            return
        menu = QMenu(self)
        menu.setObjectName('ChatMessageContextMenu')
        menu.ensurePolished()
        menu.setAttribute(Qt.WA_TranslucentBackground, False)
        menu.setAttribute(Qt.WA_NoSystemBackground, False)
        menu.setAutoFillBackground(True)
        apply_style(menu, 'QMenu#ChatMessageContextMenu {background:@background.panel@;border:1px solid @border.subtle@;border-radius:0;padding:4px;}')
        for label, file_filter in [
            (tr('ui.caption.65f95f9e63c3'), tr('ui.caption.290a46163f4d')),
            (tr('ui.plugin_manager_pages.image_a029'), tr('ui.caption.f120a500e6f6')),
            (tr('ui.caption.f92fafb4c00a'), tr('ui.caption.2b0e84ebe0d1')),
            (tr('ui.caption.1718e90d59c5'), tr('ui.caption.b55814ef6cf8')),
            (tr('ui.caption.843eaf6aa73b'), tr('ui.caption.36b521a506d5')),
        ]:
            action = add_caption_action(menu, 'addAction', label)
            action.triggered.connect(lambda checked=False, title=label, filters=file_filter:
                                     self._choose_attachment_files(title, filters))
        menu.addSeparator()
        notice = localize(add_caption_action(menu, 'addAction', 'Поддержка медиа зависит от выбранной модели'), 'ui.chat_view.media_support_depends_on_the_selected_baf1')
        notice.setEnabled(False)
        menu.ensurePolished()
        menu.adjustSize()
        anchor = self.add_btn.mapToGlobal(QPoint(0, 0))
        screen = self.add_btn.screen()
        if screen is not None:
            menu.setMaximumHeight(max(1, anchor.y() - screen.availableGeometry().top()))
        menu.exec(anchor - QPoint(0, min(menu.sizeHint().height(), menu.maximumHeight())))
        menu.deleteLater()

    def _choose_attachment_files(self, title, file_filter):
        set_popup_active(self.add_btn, True)
        try:
            paths, _ = QFileDialog.getOpenFileNames(self, tr('chat.attach_files_title', kind=title), '', file_filter, options=QFileDialog.DontUseNativeDialog)
        finally:
            set_popup_active(self.add_btn, False)
        if paths:
            self._queue_attachment_paths(paths)

    @staticmethod
    def _delete_temporary_attachment_sources(items):
        for item in items or []:
            if not item.get("_temporary_source"):
                continue
            raw_path = str(item.get("source_path") or "").strip()
            if not raw_path:
                continue
            try:
                Path(raw_path).unlink(missing_ok=True)
            except OSError:
                pass

    def _clipboard_attachment_sources(self):
        mime = QGuiApplication.clipboard().mimeData()
        if not mime:
            return [], set()
        from ui.markdown_clipboard import image_paths
        private_paths = image_paths(mime)
        if private_paths:return private_paths, set()

        # Files copied in Explorer/Dolphin/Nautilus arrive as local URLs. This
        # covers images, audio, video and text files without rewriting them.
        paths = [
            url.toLocalFile() for url in (mime.urls() if mime.hasUrls() else [])
            if url.isLocalFile()
        ]
        if paths:
            return paths, set()

        temp_dir = Path(tempfile.gettempdir()) / "toshy-clipboard-attachments"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temporary = set()
        generated = []

        # Screenshots / copied pixels normally have no filesystem path.
        if mime.hasImage():
            image = QGuiApplication.clipboard().image()
            if not image.isNull():
                target = temp_dir / f"clipboard_{time.time_ns()}.png"
                if image.save(str(target), "PNG"):
                    generated.append(str(target))
                    temporary.add(str(target.resolve()))
                    return generated, temporary

        # Some multimedia applications expose raw audio/video MIME data rather
        # than a file URL. Materialize that blob so it goes through exactly the
        # same attachment validation/storage pipeline as every other file.
        for fmt in mime.formats():
            base_mime = str(fmt).lower().split(";", 1)[0].strip()
            if not base_mime.startswith(("audio/", "video/")):
                continue
            raw = bytes(mime.data(fmt))
            if not raw:
                continue
            ext = mimetypes.guess_extension(base_mime) or (".bin")
            target = temp_dir / f"clipboard_{time.time_ns()}{ext}"
            try:
                target.write_bytes(raw)
            except OSError:
                continue
            generated.append(str(target))
            temporary.add(str(target.resolve()))
            break

        return generated, temporary

    def _paste_attachments_from_clipboard(self):
        if self._preparing_attachments or self._session_loading:
            return
        paths, temporary = self._clipboard_attachment_sources()
        if paths:
            self._queue_attachment_paths(paths, temporary_sources=temporary)

    def _queue_attachment_paths(self, paths, temporary_sources=None):
        if self._preparing_attachments or self._session_loading:
            return
        temporary_sources = {
            str(Path(path).expanduser().resolve()) for path in (temporary_sources or set())
        }
        added = False
        errors = []
        for index, raw_path in enumerate(paths or []):
            if not raw_path:
                continue
            resolved_source = str(Path(raw_path).expanduser().resolve())
            try:
                info = inspect_attachment_path(raw_path)
            except AttachmentError as error:
                errors.append(str(error))
                if resolved_source in temporary_sources:
                    try:
                        Path(resolved_source).unlink(missing_ok=True)
                    except OSError:
                        pass
                continue
            except Exception as error:
                errors.append(tr('ui.caption.26bd66fb90d6', v0=raw_path, v1=error))
                if resolved_source in temporary_sources:
                    try:
                        Path(resolved_source).unlink(missing_ok=True)
                    except OSError:
                        pass
                continue

            pending = dict(info)
            pending["pending_id"] = f"pending_{time.time_ns()}_{index}"
            if resolved_source in temporary_sources:
                pending["_temporary_source"] = True
            self._pending_attachments.append(pending)
            added = True

        if added:
            self._refresh_pending_attachments_panel()
        if errors:
            self._show_error_panel(tr('ui.caption.51fc767123e3') + "\n".join(errors))

    def _refresh_pending_attachments_panel(self):
        for position, item in enumerate(self._pending_attachments, 1):
            item['position'] = position
        self.attachments_panel.set_items(self._pending_attachments)
        if hasattr(self,"input_state"):self.input_state.draft_changed()
        if hasattr(self, 'message_selection') and self.message_selection.active:
            self.attachments_panel.hide()
        self._position_attachments_panel()
        self._position_error_panel()
        self._position_tags_bar()
        self._schedule_overlay_update()

    def _remove_pending_attachment(self, pending_id: str):
        key = str(pending_id or "")
        removed = [
            item for item in self._pending_attachments
            if str(item.get("pending_id") or item.get("source_path") or "") == key
        ]
        self._delete_temporary_attachment_sources(removed)
        self._pending_attachments[:] = [
            item for item in self._pending_attachments
            if str(item.get("pending_id") or item.get("source_path") or "") != key
        ]
        self._refresh_pending_attachments_panel()

    def _resolve_attachment_for_ui(self, attachment: Dict[str, Any]) -> Optional[str]:
        try:
            return str(resolve_attachment_path(attachment, self.current_session_id))
        except Exception:
            return None

    def _set_attachment_preparing(self, preparing: bool):
        self._preparing_attachments = bool(preparing)
        enabled = not self._preparing_attachments and not self._session_loading
        self.input_edit.setEnabled(enabled)
        raw=self.raw_request_btn.isChecked()
        self.raw_request_btn.setEnabled(enabled)
        self.add_btn.setEnabled(enabled)
        self.tags_btn.setEnabled(enabled)
        self.tags_bar.setEnabled(enabled)
        self.sys_prompt_btn.setEnabled(enabled)
        self.compressed_context_btn.setEnabled(enabled)
        self.pipeline_selector.setEnabled(enabled and not raw)
        if hasattr(self, 'input_controls'):
            self.input_controls.set_enabled(enabled)
        self.attachments_panel.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)
        self._refresh_stop_thinking_button()
        if preparing:
            set_text(self.send_btn, 'ui.chat_view.preparing_files_3130')
        elif not (self.current_worker and self.current_worker.isRunning()):
            self._set_send_button_running(False)

    def dragEnterEvent(self, event):
        mime = event.mimeData()
        if mime and mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        mime = event.mimeData()
        if mime and mime.hasUrls() and any(url.isLocalFile() for url in mime.urls()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        paths = [url.toLocalFile() for url in (mime.urls() if mime and mime.hasUrls() else []) if url.isLocalFile()]
        if paths:
            self._queue_attachment_paths(paths)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    def _commit_user_message_and_start(
        self,
        text: str,
        active_tags: List[str],
        payload_prompt: str,
        attachments: List[Dict[str, Any]],
        canvas_context: Optional[Dict[str, Any]] = None,
        file_context: Optional[Dict[str, Any]] = None,
        extra_kwargs: Optional[Dict[str, Any]] = None,
        preserve_composer: bool = False,
        reply_data: Optional[Dict[str, Any]] = None,
    ):
        # The user may have switched sessions while a very large file was being copied.
        if not self.current_session_id:
            self._set_attachment_preparing(False)
            return

        for position, attachment in enumerate(attachments, 1):
            attachment["position"] = position
        raw_request=self.raw_request_btn.isChecked()
        if raw_request:
            payload_prompt=text
            active_tags=[];attachments=[];canvas_context=None;file_context=None;reply_data=None
            extra_kwargs={'raw_request':True}
        user_turn = UserMessageTurn(
            text=text,
            display_text=(extra_kwargs or {}).get("task_status_display"),
            tags=active_tags,
            attachments=attachments,
            canvas_context=canvas_context,
            file_context=file_context,
            attachment_resolver=self._resolve_attachment_for_ui,
            parent=self.turns_container,
        )
        user_turn.delete_requested.connect(self._on_turn_delete_requested)
        user_turn.retry_requested.connect(self._on_turn_retry_requested)
        user_turn.create_branch_requested.connect(self._on_create_branch_from_turn)
        user_turn.content_edited.connect(self._sync_session_to_disk)

        count = self.turns_layout.count()
        self.turns_layout.insertWidget(count - 1, user_turn)
        history_item = {"role": "user", "content": payload_prompt}
        message_kwargs = {}
        if not raw_request and not preserve_composer and self.input_controls.full_web_action.isChecked():
            message_kwargs['full_web_pages'] = True
            user_turn.chat_meta['full_web_pages'] = True
        if not preserve_composer:
            self.input_controls.full_web_action.setChecked(False)
        if not raw_request and not preserve_composer:
            selected_skills = self.input_controls.skills.consume()
            if selected_skills:
                message_kwargs['requested_resources'] = {'skills': selected_skills}
                if not hasattr(user_turn, 'chat_meta'):
                    from ui.views.chat_references import turn_metadata
                    turn_metadata(user_turn)
                user_turn.chat_meta['requested_resources'] = dict(message_kwargs['requested_resources'])
        if active_tags:
            message_kwargs["tags"] = list(active_tags)
        if attachments:
            message_kwargs["attachments"] = [dict(item) for item in attachments]
        if canvas_context:
            message_kwargs["canvas_context"] = dict(canvas_context)
            message_kwargs["canvas_user_text"] = text
        if file_context:
            message_kwargs["file_context"] = dict(file_context)
            message_kwargs["file_user_text"] = text
        if extra_kwargs:
            message_kwargs.update(dict(extra_kwargs))
            user_turn.chat_meta.update(dict(extra_kwargs))
        from ui.views.chat_references import turn_metadata
        message_kwargs.update(turn_metadata(user_turn))
        # Подготовка вложений могла закончиться уже после отмены ответа.
        if not preserve_composer and reply_data != self.chat_references.snapshot_reply():
            reply_data = None
        if reply_data is not None:
            message_kwargs['chat_reply'] = dict(reply_data)
            user_turn.chat_meta['chat_reply'] = dict(reply_data)
            self.chat_references.attach_reply(user_turn)
            self.chat_references.consume_reply(reply_data)
        if message_kwargs:
            history_item["additional_kwargs"] = message_kwargs
        self.messages_history.append(history_item)
        self.user_turn_widgets.append(user_turn)
        self.timeline_rail.update_turns(self.user_turn_widgets)

        if not preserve_composer:
            self.input_edit.clear()
            if not raw_request:self._pending_attachments.clear()
            self._refresh_pending_attachments_panel()
            self._set_attachment_preparing(False)
        self._auto_scroll_to_bottom = True
        self.scroll_area.verticalScrollBar().setValue(self.scroll_area.verticalScrollBar().maximum())

        self._retry_mode_active = False
        self._generation_insert_index = None

        self.active_thought_turn = ThoughtMessageTurn(parent=self.turns_container)
        self.active_thought_turn.delete_requested.connect(self._on_turn_delete_requested)
        self.active_thought_turn.create_branch_requested.connect(self._on_create_branch_from_turn)
        self.active_thought_turn.content_edited.connect(self._sync_session_to_disk)

        count = self.turns_layout.count()
        self.turns_layout.insertWidget(count - 1, self.active_thought_turn)
        self.active_assistant_turn = None

        set_text(self.send_btn, 'ui.chat_view.update_task_73d3')
        self.stop_run_btn.show()
        self.send_btn.setObjectName('ChatStudioSendBtn')
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)

        include_thoughts = bool(self.current_include_thoughts_in_context)
        self.current_worker = ChatStreamWorker(
            messages=self.messages_history,
            base_url=core_process.base_url,
            session_id=self.current_session_id,
            branch=self.current_branch,
            include_thoughts_in_context=include_thoughts,
            workspace_root=self.current_workspace_root,
            temp_memory_mode=self.current_temp_memory_mode,
            raw_request=raw_request,
            parent=self
        )
        self._register_and_start_worker(self.current_worker)

    def _prepare_session_for_send(self, active_tags: List[str]) -> bool:
        """Create/update the active session before any UI surface starts a run."""
        if self._session_loading:
            return False
        if self._model_change_pending:
            self._show_error_panel(tr('ui.caption.cb19dc57ee30'))
            return False
        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False
        if self._pending_retry is not None:
            self._show_error_panel(tr('ui.caption.cbc46c7ff65d'))
            return False
        # A new/legacy chat inherits the default once. Later changes to the
        # application's default must not silently redirect this saved chat.
        self.model_selector.set_profile(self.model_selector.resolved_profile_id())
        if not self.current_session_id:
            self.current_branch = "main"
            self.current_temp_memory_mode = self.welcome_widget.selected_mode
            self.current_include_thoughts_in_context = bool(
                self.welcome_widget.include_thoughts_enabled
            )
            init_meta = {
                "temp_memory_mode": self.current_temp_memory_mode,
                "limit_ranking": True,
                "active_tags": active_tags,
                "selected_pipeline": self.pipeline_selector.selected,
                "model_profile_id": self.model_selector.resolved_profile_id(),
                "workspace_root": self.current_workspace_root,
                "include_thoughts_in_context": self.current_include_thoughts_in_context,
                "include_replied_thoughts_in_context": self.welcome_widget.include_replied_thoughts_checkbox.isChecked(),
                "reasoning_effort": self.reasoning_selector.effective_value(),
                "input_options": self.input_state.options(),
            }
            try:
                created = self.session_manager.create_session(
                    init_meta,
                    folder_id=self._pending_new_session_folder_id or app_state.expanded_project_id or app_state.project_id,
                    settings={
                        "store_temp_memory_both": bool(self.welcome_widget.store_both_enabled),
                        "system_prompt": self.welcome_widget.system_prompt_edit.toPlainText(),
                    },
                )
                self.current_session_id = created["session_id"]
                if created.get('title'):
                    app_state.session_titles[self.current_session_id] = created['title']
                    app_state.branch_titles[(self.current_session_id, 'main')] = created['title']
                    app_state.titles_changed.emit()
                self.welcome_widget.system_prompt_edit.clear()
                # _stash_session caches this metadata when leaving a new chat.
                self._session_metadata = dict(created.get('metadata') or init_meta)
                self.input_state.adopt()
                folder = created.get('folder_id')
                if folder:
                    project = app_state.folder_projects.get(folder) or app_state.project_id or folder
                    app_state.session_projects[self.current_session_id] = project
                self._pending_new_session_folder_id = None
                app_state.select_session(
                    self.current_session_id,
                    self.current_branch,
                    workspace=self.current_workspace_root,
                )
            except Exception as error:
                self._show_error_panel(tr('ui.caption.7392b0ae4c5c', v0=error))
                return False
            event_bus.sessions_updated.emit()
            self.welcome_widget.hide()
            self.scroll_area.show()
            return True

        updates = {"active_tags": active_tags, "selected_pipeline": self.pipeline_selector.selected,
                   "model_profile_id": self.model_selector.resolved_profile_id(),
                   "reasoning_effort": self.reasoning_selector.effective_value(),
                   "input_options": self.input_state.options()}
        if self.current_workspace_root:
            updates["workspace_root"] = self.current_workspace_root
        try:
            self.session_manager.update_metadata(
                self.current_session_id,
                updates,
                branch=self.current_branch,
            )
        except Exception as error:
            self._show_error_panel(tr('ui.caption.d4d89c4f6c86', v0=error))
            return False
        self._session_metadata.update(updates)
        self.input_state.remember()
        return True

    def send_external_image_message(self, text, path, finished=None):
        """Paint sends a private attachment without changing Chat's draft."""
        callback = finished or (lambda success: None)
        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            callback(False)
            return False
        if self._session_loading or self._preparing_attachments or self._pending_retry is not None or (self.current_worker and self.current_worker.isRunning()):
            self._show_error_panel(tr('ui.caption.4b238959893b'))
            callback(False)
            return False
        if app_state.session_id and (self.current_session_id != app_state.session_id or self.current_branch != app_state.branch):
            self.load_session(app_state.session_id, branch=app_state.branch)
            callback(False)
            return False
        tags = list(self.tags_bar.get_active_tags())
        message = str(text or "").strip()
        prompt = self._build_tagged_prompt(message, tags)
        if not self._prepare_session_for_send(tags):
            callback(False)
            return False
        target_session, target_branch = str(self.current_session_id), str(self.current_branch)
        self._set_attachment_preparing(True)
        def imported(item):
            self._set_attachment_preparing(False)
            if item.get("storage") == "link":
                self.attachment_runner.submit(lambda: self.session_manager.cleanup_attachments(target_session))
                self._show_error_panel(tr('ui.caption.be9614ca1b30'))
                callback(False)
                return
            if self.current_session_id != target_session or self.current_branch != target_branch or not core_process.is_running() or (self.current_worker and self.current_worker.isRunning()):
                self.attachment_runner.submit(lambda: self.session_manager.cleanup_attachments(target_session))
                callback(False)
                return
            self._clear_error_panel()
            self._commit_user_message_and_start(message, tags, prompt, [dict(item)], preserve_composer=True)
            callback(True)
        def failed(error):
            self._set_attachment_preparing(False)
            self._show_error_panel(tr('ui.caption.b233010de598', v0=error))
            callback(False)
        self.attachment_runner.submit(lambda: self.session_manager.import_attachment(target_session, str(path), force_copy=True),
                                      on_success=imported, on_error=failed)
        return True

    def send_external_message(self, text: str) -> bool:
        """Send a message produced by another workspace into the active chat session.

        Editor/Board integrations call this instead of mutating Chat's input box.
        The supplied text is already the final visible message; session tags are
        appended only to the payload exactly like a normal Chat send.
        """
        message = str(text or "").strip()
        if not message:
            return False

        self._clear_error_panel()

        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return False

        if self._preparing_attachments:
            self._show_error_panel(tr('ui.caption.ccde5f327ef8'))
            return False

        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False

        # ApplicationState is the GUI-wide owner of the selected session. In
        # normal operation ChatView is already synchronized with it, but keep
        # this entry point safe if another surface triggers a send immediately
        # after a session switch.
        if app_state.session_id and (
            self.current_session_id != app_state.session_id
            or self.current_branch != app_state.branch
            or self._session_loading
        ):
            self.load_session(app_state.session_id, branch=app_state.branch)
            if self.current_session_id != app_state.session_id:
                return False

        active_tags = list(self.tags_bar.get_active_tags())
        payload_prompt = self._build_tagged_prompt(message, active_tags)
        if not self._prepare_session_for_send(active_tags):
            return False

        self._commit_user_message_and_start(message, active_tags, payload_prompt, [])
        return True

    def send_task_list(self, session_id: str, branch: str, tasks: List[Dict[str, Any]]) -> bool:
        active_tasks = [dict(task) for task in (tasks or []) if str(task.get("status") or "") == "in_progress"]
        if not session_id or not active_tasks:
            return False
        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return False
        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False

        target_branch = branch or "main"
        if self._session_loading or self.current_session_id != session_id or self.current_branch != target_branch:
            self.load_session(session_id, branch=target_branch)
            if self._session_loading or self.current_session_id != session_id or self.current_branch != target_branch:
                return False

        prompt = build_active_task_list_prompt(active_tasks)
        active_tags = list(self.tags_bar.get_active_tags())
        payload_prompt = self._build_tagged_prompt(prompt, active_tags)
        if not self._prepare_session_for_send(active_tags):
            return False

        self._commit_user_message_and_start(
            prompt,
            active_tags,
            payload_prompt,
            [],
            preserve_composer=True,
        )
        return True

    def send_task_status_check(self, session_id: str, branch: str, tasks: List[Dict[str, Any]]) -> bool:
        active_tasks = [dict(task) for task in (tasks or []) if str(task.get("status") or "") == "in_progress"]
        if not session_id or not active_tasks:
            return False
        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return False
        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False

        target_branch = branch or "main"
        if self._session_loading or self.current_session_id != session_id or self.current_branch != target_branch:
            self.load_session(session_id, branch=target_branch)
            if self.current_session_id != session_id:
                return False

        prompt = build_task_status_prompt(active_tasks)
        active_tags = list(self.tags_bar.get_active_tags())
        payload_prompt = self._build_tagged_prompt(prompt, active_tags)
        if not self._prepare_session_for_send(active_tags):
            return False

        self._commit_user_message_and_start(
            prompt,
            active_tags,
            payload_prompt,
            [],
            extra_kwargs={
                "task_status_check": True,
                "task_status_display": "Проверь статусы задач",
                "task_status_task_ids": [
                    str(task.get("id") or "") for task in active_tasks if task.get("id")
                ],
            },
            preserve_composer=True,
        )
        if self.current_worker is not None:
            self.current_worker.task_status_check = {
                "session_id": session_id,
                "task_ids": [str(task.get("id") or "") for task in active_tasks if task.get("id")],
            }
        return True

    def _apply_task_status_check_result(self, worker: ChatStreamWorker):
        check = getattr(worker, "task_status_check", None)
        if not isinstance(check, dict):
            return
        session_id = str(check.get("session_id") or "")
        success = bool(getattr(worker, "task_status_report_success", False))

        if not success and self.current_session_id == session_id:
            output = str(getattr(worker, "task_status_report_output", "") or "").strip()
            if output.startswith("System Error:"):
                self._add_system_message(
                    tr('ui.caption.eb6c11553ed2')
                )
            else:
                self._add_system_message(
                    tr('ui.caption.a3856d3b6c18')
                )

        event_bus.sessions_updated.emit()

    def send_external_file_message(self, text: str, file_context: Dict[str, Any]) -> bool:
        """Send an editor request with a hidden machine-readable filesystem path."""
        message = str(text or "").strip()
        context = dict(file_context or {})
        if not message:
            return False

        self._clear_error_panel()

        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return False

        if self._preparing_attachments:
            self._show_error_panel(tr('ui.caption.ccde5f327ef8'))
            return False

        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False

        if app_state.session_id and (
            self.current_session_id != app_state.session_id
            or self.current_branch != app_state.branch
            or self._session_loading
        ):
            self.load_session(app_state.session_id, branch=app_state.branch)
            if self.current_session_id != app_state.session_id:
                return False

        active_tags = list(self.tags_bar.get_active_tags())
        file_prompt = self._build_file_context_prompt(message, context)
        payload_prompt = self._build_tagged_prompt(file_prompt, active_tags)
        if not self._prepare_session_for_send(active_tags):
            return False

        self._commit_user_message_and_start(
            message,
            active_tags,
            payload_prompt,
            [],
            file_context=context,
        )
        return True

    def send_external_canvas_message(self, text: str, canvas_context: Dict[str, Any]) -> bool:
        """Send a Board request using a hidden structured canvas payload.

        Chat renders `canvas_context` as a lightweight projection, while the
        model receives the compact JSON plus an English explanation of its
        directed-graph semantics.
        """
        message = str(text or "").strip()
        context = dict(canvas_context or {})
        if not message and not context.get("nodes"):
            return False

        self._clear_error_panel()

        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return False

        if self._preparing_attachments:
            self._show_error_panel(tr('ui.caption.ccde5f327ef8'))
            return False

        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return False

        if app_state.session_id and (
            self.current_session_id != app_state.session_id
            or self.current_branch != app_state.branch
            or self._session_loading
        ):
            self.load_session(app_state.session_id, branch=app_state.branch)
            if self.current_session_id != app_state.session_id:
                return False

        active_tags = list(self.tags_bar.get_active_tags())
        canvas_prompt = self._build_canvas_context_prompt(message, context)
        payload_prompt = self._build_tagged_prompt(canvas_prompt, active_tags)
        if not self._prepare_session_for_send(active_tags):
            return False

        self._commit_user_message_and_start(
            message,
            active_tags,
            payload_prompt,
            [],
            canvas_context=context,
        )
        return True

    def wheelEvent(self, event):
        if self.scroll_area.isVisible():
            from PySide6.QtGui import QWheelEvent
            viewport = self.scroll_area.viewport()
            forwarded = QWheelEvent(
                viewport.mapFromGlobal(event.globalPosition().toPoint()),
                event.globalPosition(), event.pixelDelta(), event.angleDelta(),
                event.buttons(), event.modifiers(), event.phase(), event.inverted(),
            )
            QApplication.sendEvent(viewport, forwarded)
            event.accept()
            return
        super().wheelEvent(event)

    def _send_message(self):
        if self._session_loading or self._preparing_attachments or self._pending_retry is not None:
            return
        if self.current_worker and self.current_worker.isRunning():
            from ui.views.chat_steering import send_update
            send_update(self)
            return

        text = self.input_edit.toPlainText() if self.raw_request_btn.isChecked() else self.input_edit.toPlainText().strip()
        reply_data = self.chat_references.snapshot_reply()
        if not text and not self._pending_attachments:
            return

        # Errors are request-scoped UI state, not chat history. Starting the
        # next request always dismisses the previous banner first.
        self._clear_error_panel()

        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено. Запустите ядро в нижней панели."))
            return

        active_tags = list(self.tags_bar.get_active_tags())
        if self.raw_request_btn.isChecked():
            if not text.strip():return
            if self._prepare_session_for_send([]):
                self._commit_user_message_and_start(text,[],text,[])
            return
        payload_prompt = self._build_tagged_prompt(text, active_tags)

        if not self._prepare_session_for_send(active_tags):
            return

        if not self._pending_attachments:
            self._commit_user_message_and_start(text, active_tags, payload_prompt, [], reply_data=reply_data)
            return

        pending_snapshot = [dict(item) for item in self._pending_attachments]
        target_session_id = str(self.current_session_id)
        target_branch = str(self.current_branch)
        self._set_attachment_preparing(True)

        def import_batch():
            imported = []
            try:
                for item in pending_snapshot:
                    options = {"force_copy": True} if item.get("_temporary_source") or item.get("_draft_source") else {}
                    attachment = self.session_manager.import_attachment(
                        target_session_id, str(item.get("source_path") or ""), **options,
                        to_memory=bool(item.get('to_memory')), branch=target_branch,
                        request_id=item.get('pending_id') or ''
                    )
                    if options and attachment.get("storage") == "link":
                        raise AttachmentError("Ядро не сохранило копию временного вложения. Обновите и перезапустите ядро агента.")
                    imported.append(attachment)
                return imported
            except Exception:
                try:
                    self.session_manager.cleanup_attachments(target_session_id)
                except Exception:
                    pass
                raise

        def on_imported(imported):
            self._delete_temporary_attachment_sources(pending_snapshot)
            if self.current_session_id != target_session_id or self.current_branch != target_branch:
                self._set_attachment_preparing(False)
                self.attachment_runner.submit(
                    lambda: self.session_manager.cleanup_attachments(target_session_id)
                )
                return
            self._commit_user_message_and_start(
                text, active_tags, payload_prompt, [dict(item) for item in (imported or [])], reply_data=reply_data
            )

        def on_import_error(error):
            self._set_attachment_preparing(False)
            self._show_error_panel(tr('ui.caption.c1785aa084e1', v0=error))

        self.attachment_runner.submit(import_batch, on_success=on_imported, on_error=on_import_error)

    def _ensure_active_thought_turn(self) -> ThoughtMessageTurn:
        if self.active_thought_turn is None:
            target = getattr(self, '_resume_target', None)
            index = self.turns_layout.indexOf(target) if isinstance(target, AssistantMessageTurn) else None
            self.active_thought_turn = self._create_thought_turn(insert_index=index)
        return self.active_thought_turn

    def _on_thought_chunk(self, chunk: str):
        turn = self._ensure_active_thought_turn()
        if getattr(self, '_resume_target', None) is not None and not turn._timer.isActive():
            turn.start_time = time.time() - turn.elapsed_seconds
            turn.resume_thinking()
        turn.append_thought(chunk)

    def _refresh_stop_thinking_button(self):
        worker = self.current_worker
        state = getattr(worker, 'reasoning_control_state', {}) or {}
        enabled = bool(worker and state.get('available') and not self._session_loading
                       and not self._preparing_attachments and self._thinking_control_pending is not worker
                       and not getattr(worker, 'was_cancelled', False))
        self.stop_thinking_btn.setEnabled(enabled)
        bind_caption(self.stop_thinking_btn, state.get('message') or
            tr('ui.chat_view.finish_the_current_work_and_provide_31fd'), setter='setToolTip')

    def _on_reasoning_control(self, state):
        self._refresh_stop_thinking_button()

    def _stop_thinking(self):
        worker = self.current_worker
        if worker is None or not self.stop_thinking_btn.isEnabled():
            return
        control_id = worker.reasoning_control_state.get('control_id')
        self._thinking_control_pending = worker
        set_text(self.stop_thinking_btn, 'ui.chat_view.finishing_work_5273', setter='setToolTip')
        self.stop_thinking_btn.setEnabled(False)

        def finished(result):
            if self._thinking_control_pending is not worker:
                return
            self._thinking_control_pending = None
            bind_caption(self.stop_thinking_btn, '')
            if self.current_worker is worker:
                self._refresh_stop_thinking_button()
                if not result.get('success') and worker.reasoning_control_state.get('control_id') == control_id:
                    self._show_error_panel(result.get('message') or tr('ui.caption.8c99321afa3f'))

        self.reasoning_control_runner.submit(lambda: worker.stop_thinking(control_id), on_success=finished,
            on_error=lambda error: finished({'success': False, 'message': tr('ui.caption.7921394fd4fc')}))

    def _on_message_identity(self, kind: str, identity: str):
        from core.chat_references import valid_chat_id
        if not valid_chat_id(identity):
            return
        if kind == 'assistant':
            if self.active_assistant_turn is None:
                # Identity arrives before its content. Do not create an empty
                # message for a tool-only pass or a whitespace-only delta.
                self._pending_assistant_identity = identity
                return
            turn = self.active_assistant_turn
        else:
            turn = self._ensure_active_thought_turn()
        turn.chat_meta['chat_id'] = identity
        self.chat_references.invalidate_turn_index()

    def _on_tool_started(self, tool_name: str, args: dict, tool_id: str = ""):
        self._ensure_active_thought_turn().add_tool_start(tool_name, args, tool_id)

    def _tool_owner(self, tool_id):
        for index in range(self.turns_layout.count() - 1, -1, -1):
            turn = self.turns_layout.itemAt(index).widget()
            if isinstance(turn, ThoughtMessageTurn) and any(
                    segment.get('id') == tool_id for segment in turn.transcript_segments if segment.get('type') == 'tool'):
                return turn
        return self._ensure_active_thought_turn()

    def _on_tool_finished(self, tool_id: str, tool_name: str, result: str, status="completed"):
        self._tool_owner(tool_id).add_tool_result(tool_id, tool_name, result, status)

    def _set_embedding_warning(self, message: str):
        text = str(message or tr('ui.caption.f5e751b8b32c')).strip()
        self._embedding_warning_message = text
        if hasattr(self, "embedding_warning_btn"):
            set_text(self.embedding_warning_btn, 'ui.chat_view.embedding_error_v0_c1b3', setter='setToolTip', v0=text)
            self.embedding_warning_btn.show()

    def _clear_embedding_warning(self):
        self._embedding_warning_message = ""
        if hasattr(self, "embedding_warning_btn"):
            self.embedding_warning_btn.hide()
            bind_caption(self.embedding_warning_btn, "", setter='setToolTip')

    def _on_embedding_status(self, active: bool, message: str):
        if active:
            self._set_embedding_warning(message)
        else:
            self._clear_embedding_warning()

    def _on_embedding_error(self, message: str):
        text = str(message or tr('ui.caption.f5e751b8b32c')).strip()
        # Реальный tool-вызов упал: оставляем постоянный индикатор и дополнительно
        # показываем transient banner до следующего запроса/ручного закрытия.
        self._set_embedding_warning(text)
        if text in self._embedding_errors_seen:
            return
        self._embedding_errors_seen.add(text)
        self._show_error_panel(tr('ui.caption.588021c8857d', v0=text))

    def _on_content_chunk(self, chunk: str):
        if not chunk:
            return
        if self.active_assistant_turn is None:
            self._pending_assistant_prefix += chunk
            if not self._pending_assistant_prefix.strip():
                return
            chunk = self._pending_assistant_prefix
            self._pending_assistant_prefix = ""

        if self.active_thought_turn:
            try:
                self.active_thought_turn.auto_collapse()
            except RuntimeError:
                pass
            self.active_thought_turn = None

        if not self.active_assistant_turn:
            self.active_assistant_turn = self._create_assistant_turn(segment_kind="streaming")
            if self._pending_assistant_identity:
                self.active_assistant_turn.chat_meta['chat_id'] = self._pending_assistant_identity
                self.chat_references.invalidate_turn_index()
            self._pending_assistant_identity = None

        self.active_assistant_turn.append_content(chunk)

    def _on_content_interim(self):
        """Close the current streamed text as an interim assistant segment."""
        self._pending_assistant_identity = None
        self._pending_assistant_prefix = ""
        if self.active_assistant_turn is not None:
            turn = self.active_assistant_turn
            turn.md_view.flush_pending_markdown()
            if not turn.raw_content.strip():
                self.turns_layout.removeWidget(turn)
                turn.deleteLater()
            else:
                turn.segment_kind = "interim"
                turn.stamp_current_time()
                self.active_thought_turn = None
            self.active_assistant_turn = None

        # Do not create an empty thinking card eagerly. A subsequent reasoning or
        # real tool event will create one on demand.

    def _on_content_reset(self):
        """Discard non-conversational fallback tool syntax streamed as text."""
        self._pending_assistant_identity = None
        self._pending_assistant_prefix = ""
        turn = self.active_assistant_turn
        self.active_assistant_turn = None
        if turn is not None:
            try:
                self.turns_layout.removeWidget(turn)
                turn.deleteLater()
            except RuntimeError:
                pass

        self._ensure_active_thought_turn()

    def _fold_finished_run(self, worker):
        segments = getattr(worker, 'completed_segments', None)
        if segments is None:
            return
        if any(segment.get('type') == 'user_update' for segment in segments):
            from ui.views.chat_steering import rebuild_run
            rebuild_run(self, worker, segments)
            return
        owned = [self.turns_layout.itemAt(index).widget() for index in range(self.turns_layout.count())]
        owned = [turn for turn in owned if isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn))
                 and (getattr(turn, '_run_state', None) or {}).get('run_id') == worker.run_id]
        thought_segments = [dict(item) for item in segments if item.get('type') in {'reasoning', 'tool'}]
        final_segment = next((item for item in reversed(segments) if item.get('type') == 'assistant_final'), None)
        first = next((turn for turn in owned if isinstance(turn, ThoughtMessageTurn) and not isinstance(turn, SubagentMessageTurn)), None)
        final = next((turn for turn in reversed(owned) if isinstance(turn, AssistantMessageTurn)), None)
        if thought_segments:
            if first is None:
                index = self.turns_layout.indexOf(owned[0]) if owned else None
                first = self._create_thought_turn(insert_index=index, defer_body=True)
                first._run_state = dict(worker.result_state or {'run_id': worker.run_id, 'status': 'completed'})
            # Keep the first card's identity and collapse state. Rebuild its body
            # lazily instead of moving widgets from unrelated saved turns.
            first._reset_body_widgets()
            first.transcript_segments = thought_segments
            first.total_thought_text = '\n'.join(str(item.get('text') or '') for item in thought_segments if item.get('type') == 'reasoning')
            first.tools_count = sum(item.get('type') == 'tool' for item in thought_segments)
            first._defer_body = True
            first.finish_thinking(sum(float(item.get('phase_seconds') or 0) for item in thought_segments))
            if first.is_expanded:
                first._materialize_thought_body()
        if final_segment:
            if final is None:
                final = self._create_assistant_turn()
                final._run_state = dict(worker.result_state or {'run_id': worker.run_id, 'status': 'completed'})
            final.raw_content = str(final_segment.get('text') or '')
            final.md_view.set_markdown(final.raw_content)
            final.segment_kind = 'final'
        subagent_segment = next((s for s in segments if s.get('type') == 'subagents'), None)
        child_turn = next((turn for turn in owned if isinstance(turn, SubagentMessageTurn)), None)
        if subagent_segment:
            if child_turn is None:child_turn = self._create_subagent_turn()
            child_turn.set_results(subagent_segment.get('results') or [])
            child_turn._run_state = dict(worker.result_state or {'run_id':worker.run_id, 'status':'completed'})
        for turn in owned:
            if turn is first or (subagent_segment and turn is child_turn) or (final_segment and turn is final):
                continue
            self.turns_layout.removeWidget(turn)
            turn.hide()
            turn.deleteLater()
        if first and final_segment and final and self.turns_layout.indexOf(first) > self.turns_layout.indexOf(final):
            index = self.turns_layout.indexOf(final)
            self.turns_layout.removeWidget(first)
            self.turns_layout.insertWidget(index, first)
        if subagent_segment:
            self.turns_layout.removeWidget(child_turn)
            index = self.turns_layout.indexOf(final) if final_segment else self.turns_layout.indexOf(first)+1 if first else self.turns_layout.count()-1
            self.turns_layout.insertWidget(max(0,index), child_turn)
        self.active_thought_turn = first if thought_segments else None
        self.active_assistant_turn = final if final_segment else None

    def _on_finished(self):
        self._restore_answer_preview()
        if self.active_assistant_turn is not None:
            self.active_assistant_turn.md_view.flush_pending_markdown()
        if self.active_thought_turn:
            try:
                self.active_thought_turn.finish_thinking()
                if not self.active_thought_turn.total_thought_text.strip() and self.active_thought_turn.tools_count == 0:
                    self.turns_layout.removeWidget(self.active_thought_turn)
                    self.active_thought_turn.deleteLater()
                    self.active_thought_turn = None
            except RuntimeError:
                self.active_thought_turn = None

        if self.active_assistant_turn:
            if self.active_assistant_turn.raw_content.strip():
                self.active_assistant_turn.segment_kind = "final"
                self.active_assistant_turn.stamp_current_time()
            else:
                self.turns_layout.removeWidget(self.active_assistant_turn)
                self.active_assistant_turn.deleteLater()
                self.active_assistant_turn = None

        if not self._retry_mode_active:
            # A subsequent retry uses this in-memory history. Keep the same
            # identities and interim segments that the server saved to disk.
            completed_turns = []
            for index in range(self.turns_layout.count()):
                turn = self.turns_layout.itemAt(index).widget()
                if isinstance(turn, UserMessageTurn):
                    completed_turns = []
                elif isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn)):
                    completed_turns.append(turn)
            _, completed_history = self._snapshot_turn_messages(completed_turns)
            if getattr(self, '_resume_target', None) is not None:
                _, self.messages_history = self._snapshot_turn_messages()
            else:
                self.messages_history.extend(completed_history)

        was_retry = self._retry_mode_active
        self._finish_generation()
        self.chat_references.refresh()
        if was_retry:
            # Retry is generated from history only up to the target user turn,
            # but the visible later conversation is intentionally preserved.
            # The old answer was removed before starting; now save its replacement.
            QTimer.singleShot(50, self._sync_session_to_disk)
        event_bus.sessions_updated.emit()

    def _on_turn_delete_requested(self, turn_widget: QWidget):
        if self.current_worker and self.current_worker.isRunning():
            self._show_error_panel(tr('ui.caption.67210f3aa351'))
            return
        self.message_selection.turns()
        self.message_selection.remove_turns([turn_widget])
        self.message_selection.prune_selection()
        QTimer.singleShot(50, self._sync_session_to_disk)

    def _on_turn_retry_requested(self, user_turn: UserMessageTurn):
        if self._preparing_attachments or self._pending_retry is not None:
            return
        self._clear_error_panel()
        if user_turn not in self.user_turn_widgets:
            return
        if self._current_run_key() in self._active_runs:
            self._show_error_panel(tr('ui.caption.5bf311d73d73'))
            return
        if not core_process.is_running():
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено."))
            return

        layout_idx = -1
        for i in range(self.turns_layout.count()):
            if self.turns_layout.itemAt(i).widget() == user_turn:
                layout_idx = i
                break

        if layout_idx == -1:
            return

        # Remove all model segments belonging to this request, even when a
        # service widget separates them. Never cross the next user request.
        index = layout_idx + 1
        while index < self.turns_layout.count():
            next_w = self.turns_layout.itemAt(index).widget()
            if isinstance(next_w, UserMessageTurn):
                break
            if isinstance(next_w, (ThoughtMessageTurn, AssistantMessageTurn)):
                self.turns_layout.removeWidget(next_w)
                next_w.deleteLater()
            else:
                index += 1

        prefix_turns = [self.turns_layout.itemAt(i).widget() for i in range(layout_idx + 1)]
        _, history_slice = self._snapshot_turn_messages(prefix_turns, include_prefix=True)
        run_key = self._current_run_key()
        token = object()
        self._pending_retry = token
        set_text(self.send_btn, 'ui.chat_view.saving_0296')
        self.send_btn.setEnabled(False)

        def start_retry():
            if self._pending_retry is not token:
                return
            self._pending_retry = None
            if self._current_run_key() != run_key or user_turn not in self.user_turn_widgets:
                self._set_send_button_running(False)
                return
            self._retry_mode_active = True
            self._generation_insert_index = self.turns_layout.indexOf(user_turn) + 1
            self.active_thought_turn = self._create_thought_turn()
            self.active_assistant_turn = None
            self._set_send_button_running(True)
            self.current_worker = ChatStreamWorker(
                messages=history_slice,
                base_url=core_process.base_url,
                session_id=self.current_session_id,
                branch=self.current_branch,
                include_thoughts_in_context=bool(self.current_include_thoughts_in_context),
                workspace_root=self.current_workspace_root,
                temp_memory_mode=self.current_temp_memory_mode,
                persist_result=False,
                use_provided_history=True,
                raw_request=bool(next((m.get('additional_kwargs',{}).get('raw_request') for m in reversed(history_slice) if m.get('role')=='user'),False)),
                parent=self,
            )
            self._register_and_start_worker(self.current_worker)

        def abort_retry(_error):
            if self._pending_retry is token:
                self._pending_retry = None
                self._set_send_button_running(False)

        # Persist deletion before the stream takes the core's session lock.
        # An error, cancellation or reload must not resurrect the old answer.
        self._sync_session_to_disk(on_saved=start_retry, on_failed=abort_retry)

    def _on_create_branch_from_turn(self, turn_widget: QWidget):
        if not self.current_session_id:
            QMessageBox.information(self, tr('ui.chat_view.branches_a9a2'), tr('ui.chat_view.start_a_conversation_and_create_a_9620'))
            return

        # Название появится после первого нового запроса; ID остаётся неизменным.
        import uuid
        clean_branch = 'branch_' + uuid.uuid4().hex[:12]

        fork_msg_idx = self._history_before
        for i in range(self.turns_layout.count()):
            w = self.turns_layout.itemAt(i).widget()
            if isinstance(w, UserMessageTurn) and not getattr(w, '_steering_message', None):
                fork_msg_idx += 1
            elif isinstance(w, AssistantMessageTurn) and getattr(w, "segment_kind", "final") != "interim":
                fork_msg_idx += 1
            if w == turn_widget:
                break

        res = self.session_manager.create_branch(
            session_id=self.current_session_id,
            branch_name=clean_branch,
            source_branch=self.current_branch,
            fork_index=fork_msg_idx
        )

        if res.get("success"):
            event_bus.sessions_updated.emit()
            self.load_session(self.current_session_id, branch=clean_branch)
        else:
            QMessageBox.warning(self, tr('ui.chat_view.could_not_create_branch_48fe'), res.get("message"))

    def set_workspace_root(self, workspace_root: str):
        self.current_workspace_root = workspace_root or None

    def _on_session_renamed(self, old_name: str, new_name: str):
        for key in list(self._session_cache):
            if key[0] == old_name:
                self._session_cache[(new_name, key[1])] = self._session_cache.pop(key)
        if self.current_session_id != old_name:
            return
        self.current_session_id = new_name
        app_state.select_session(new_name, self.current_branch, self.current_workspace_root)

    def _on_branch_renamed(self, session_id: str, old_name: str, new_name: str):
        key = (session_id, old_name)
        if key in self._session_cache:
            self._session_cache[(session_id, new_name)] = self._session_cache.pop(key)
        if self.current_session_id != session_id or self.current_branch != old_name:
            return
        self.current_branch = new_name
        app_state.select_session(session_id, new_name, self.current_workspace_root)

    def _on_session_memory_mode_changed(self, session_id: str, mode: str):
        if self.current_session_id == session_id:
            self.current_temp_memory_mode = "vector" if mode == "vector" else "text"

    def _on_session_thoughts_context_changed(self, session_id: str, enabled: bool):
        enabled = bool(enabled)
        # Every cached branch inherits this session-wide setting. Otherwise a
        # later switch restores stale metadata and silently rechecks the option.
        for (cached_session, _), entry in self._session_cache.items():
            if cached_session == session_id:
                entry.setdefault('metadata', {})['include_thoughts_in_context'] = enabled
        if self.current_session_id == session_id:
            self.current_include_thoughts_in_context = enabled
            self._session_metadata['include_thoughts_in_context'] = enabled

    def _on_session_replied_thoughts_context_changed(self, session_id: str, enabled: bool):
        enabled = bool(enabled)
        for (cached_session, _), entry in self._session_cache.items():
            if cached_session == session_id:
                entry.setdefault('metadata', {})['include_replied_thoughts_in_context'] = enabled
        if self.current_session_id == session_id:
            self._session_metadata['include_replied_thoughts_in_context'] = enabled

    def _on_error(self, err: str):
        self._restore_answer_preview()
        if self.active_thought_turn:
            self.active_thought_turn.finish_thinking()
        if err == "CONNECTION_OFFLINE":
            self._show_error_panel(tr("chat_core_offline_hint", "Ядро агента отключено."))
        elif str(err).startswith("ATTACHMENT_ERROR::"):
            self._show_error_panel(tr('ui.caption.cac48c8ef253', v0=str(err).split('::', 1)[1]))
        else:
            self._show_error_panel(tr('ui.caption.47eaa39a9c97', v0=err))
        self._sync_session_to_disk()
        self._finish_generation()

    def _resume_chat_turn(self, turn, mode='native'):
        if self._session_loading or self._active_runs.get(self._current_run_key()):
            return
        state = dict(getattr(turn, '_run_state', {}) or {})
        if state.get('raw_request'):
            self._show_error_panel(tr('ui.caption.eb405e5a4ebd'))
            return
        run_id = state.get('run_id')
        if not run_id or not self.current_session_id:
            return
        self._resume_target = turn
        if getattr(turn, '_pause_placeholder', False):
            turn._pause_placeholder = False
            turn.md_view.set_markdown(turn.raw_content)
        self._retry_mode_active = False
        self._generation_insert_index = None
        self.active_thought_turn = turn if isinstance(turn, ThoughtMessageTurn) else None
        self.active_assistant_turn = turn if isinstance(turn, AssistantMessageTurn) else None
        if isinstance(turn, AssistantMessageTurn):
            self._hide_answer_preview(turn)
            target_index = self.turns_layout.indexOf(turn)
            for index in range(target_index - 1, -1, -1):
                candidate = self.turns_layout.itemAt(index).widget()
                if isinstance(candidate, UserMessageTurn):
                    break
                if isinstance(candidate, ThoughtMessageTurn):
                    self.active_thought_turn = candidate
                    break
        else:
            turn.start_time = time.time() - turn.elapsed_seconds
            turn.resume_thinking()
        self.current_worker = ChatStreamWorker(
            messages=[], base_url=core_process.base_url, session_id=self.current_session_id,
            branch=self.current_branch, include_thoughts_in_context=bool(self.current_include_thoughts_in_context),
            workspace_root=self.current_workspace_root, temp_memory_mode=self.current_temp_memory_mode,
            resume_run_id=run_id, resume_mode=mode, parent=self)
        self.current_worker.resume_model_profile_id = str(state.get('model_profile_id') or '')
        self.current_worker.reasoning_effort = str(state.get('reasoning_effort') or '')
        self._set_send_button_running(True)
        self._register_and_start_worker(self.current_worker)
        from ui.views.chat_resume import refresh_controls
        refresh_controls(self)

    def _hide_answer_preview(self, turn):
        # Hide immediately, but keep the saved partial until replacement text
        # arrives. A rejected request/Stop must not destroy the old response.
        turn._answer_preview_hidden = True
        turn.md_view.set_markdown('')

    def _restore_answer_preview(self):
        target = getattr(self, '_resume_target', None)
        if isinstance(target, AssistantMessageTurn) and getattr(target, '_answer_preview_hidden', False):
            target._answer_preview_hidden = False
            target.md_view.set_markdown(target.raw_content)

    def _on_answer_continue(self):
        self._restore_answer_preview()
        target = getattr(self, '_resume_target', None)
        if isinstance(target, AssistantMessageTurn):
            target.segment_kind = 'streaming'
            self.active_assistant_turn = target

    def _on_answer_restart(self):
        target = getattr(self, '_resume_target', None)
        if isinstance(target, AssistantMessageTurn):
            target._answer_preview_hidden = False
            target.raw_content = ''
            target.md_view.set_markdown('')
            target.segment_kind = 'streaming'
            self.active_assistant_turn = target

    def _on_run_state(self, state):
        from ui.views.chat_resume import mark_run
        mark_run(self, state['run_id'], state)

    def _show_continuation_fallback(self, turn):
        box = QMessageBox(self)
        set_text(box, 'ui.chat_view.continue_generation_92a6', setter='setWindowTitle')
        set_text(box, 'ui.chat_view.the_server_rejected_message_continuation_saved_7a31')
        logical = box.addButton(tr('ui.caption.fc67ca3605d8'), QMessageBox.AcceptRole)
        regenerate = box.addButton(tr('ui.caption.d0ebfbccd47a'), QMessageBox.DestructiveRole)
        box.addButton(tr('common.cancel'), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is logical:
            self._resume_chat_turn(turn, mode='logical')
        elif box.clickedButton() is regenerate:
            users = [self.turns_layout.itemAt(i).widget() for i in range(self.turns_layout.count())
                     if isinstance(self.turns_layout.itemAt(i).widget(), UserMessageTurn)]
            if users:
                self._on_turn_retry_requested(users[-1])

    def _current_run_key(self):
        if not self.current_session_id:
            return None
        return self.current_session_id, self.current_branch

    def _register_and_start_worker(self, worker: ChatStreamWorker):
        self._clear_error_panel()
        run_key = self._current_run_key()
        if not run_key:
            return
        worker.pipeline = '' if getattr(worker,'raw_request',False) else self.pipeline_selector.selected
        from ui.views.chat_request_badges import snapshot_request_badges
        snapshot_request_badges(self, worker)
        if not worker.resume_run_id:
            worker.reasoning_effort = self.reasoning_selector.effective_value()
        worker.model_profile_id = self.model_selector.resolved_profile_id()
        worker.message_identity.connect(
            lambda kind, identity, key=run_key, w=worker: self._route_run_event(key, w, self._on_message_identity, kind, identity))
        worker.pipeline_notification.connect(lambda payload, key=run_key, w=worker: self._route_run_event(key, w, lambda event: render_notification(self, event), payload))
        worker.pipeline_paused.connect(lambda payload, key=run_key, w=worker: self._route_run_event(key, w, self._show_pipeline_pause, payload))
        self._active_runs[run_key] = worker
        self.current_worker = worker
        from ui.views.chat_resume import mark_run
        mark_run(self, worker.run_id, {'status': 'running',
            'model_profile_id': worker.resume_model_profile_id or worker.model_profile_id})
        from ui.views.chat_steering import handle_event
        worker.steering_event.connect(lambda event, key=run_key, w=worker: self._route_run_event(key, w, handle_event, self, event, w))
        worker.subagent_event.connect(lambda payload, key=run_key, w=worker: self._route_run_event(key, w, self._on_subagent_event, payload))
        worker.run_state_received.connect(lambda state, key=run_key, w=worker: self._route_run_event(key, w, self._on_run_state, state))
        worker.reasoning_control_received.connect(lambda state, key=run_key, w=worker: self._route_run_event(key, w, self._on_reasoning_control, state))
        worker.answer_restart_received.connect(lambda key=run_key, w=worker: self._route_run_event(key, w, self._on_answer_restart))
        worker.answer_continue_received.connect(lambda key=run_key, w=worker: self._route_run_event(key, w, self._on_answer_continue))
        self._detached_runs.discard(run_key)
        worker.thought_chunk_received.connect(
            lambda chunk, key=run_key, w=worker: self._route_run_event(key, w, self._on_thought_chunk, chunk)
        )
        worker.tool_started.connect(
            lambda name, args, tool_id, key=run_key, w=worker: self._route_run_event(
                key, w, self._on_tool_started, name, args, tool_id
            )
        )
        worker.tool_finished.connect(
            lambda tool_id, name, result, key=run_key, w=worker: self._route_run_event(
                key, w, self._on_tool_finished, tool_id, name, result, w.tool_result_statuses.get(tool_id, "completed")
            )
        )
        worker.tool_sources_received.connect(
            lambda tool_id, sources, key=run_key, w=worker: self._route_run_event(
                key, w, lambda identity, items: self._tool_owner(identity).add_tool_sources(identity, items), tool_id, sources)
        )
        worker.content_chunk_received.connect(
            lambda chunk, key=run_key, w=worker: self._route_run_event(key, w, self._on_content_chunk, chunk)
        )
        worker.content_interim_received.connect(
            lambda key=run_key, w=worker: self._route_run_event(key, w, self._on_content_interim)
        )
        worker.content_reset_received.connect(
            lambda key=run_key, w=worker: self._route_run_event(key, w, self._on_content_reset)
        )
        worker.embedding_error_received.connect(
            lambda message, key=run_key, w=worker: self._route_run_event(
                key, w, self._on_embedding_error, message
            )
        )
        worker.embedding_status_received.connect(
            lambda active, message, key=run_key, w=worker: self._route_run_event(
                key, w, self._on_embedding_status, active, message
            )
        )
        worker.stream_finished.connect(lambda key=run_key, w=worker: self._on_run_finished(key, w))
        worker.error_occurred.connect(lambda error, key=run_key, w=worker: self._on_run_error(key, w, error))
        self._update_generation_status('running', run_key)
        worker.start()

    def _update_generation_status(self, outcome, run_key):
        from ui.generation_status import generation_status
        generation_status.set(*run_key, outcome)

    def _route_run_event(self, run_key, worker, handler, *args):
        if self._active_runs.get(run_key) is not worker:
            return
        if run_key != self._current_run_key() or run_key in self._detached_runs:
            return
        handler(*args)

    def _on_run_finished(self, run_key, worker):
        if self._active_runs.get(run_key) is not worker:
            return
        if run_key in self._session_cache:
            self._session_cache[run_key]['stale'] = True
        self._active_runs.pop(run_key, None)
        self._update_generation_status('stopped' if bool(getattr(worker, 'was_cancelled', False)) else 'finished', run_key)
        was_detached = run_key in self._detached_runs
        self._detached_runs.discard(run_key)
        if run_key == self._current_run_key() and not was_detached:
            was_cancelled = bool(getattr(worker, "was_cancelled", False))
            from ui.views.chat_resume import mark_run
            state = worker.result_state or {'status': 'stopped' if was_cancelled else 'completed'}
            if was_cancelled and (self.active_thought_turn is None or (
                    not self.active_thought_turn.total_thought_text and not self.active_thought_turn.tools_count)) and self.active_assistant_turn is None:
                self.active_assistant_turn = self._create_assistant_turn()
                self.active_assistant_turn.append_content(tr('ui.caption.f45eaf777d05'))
            mark_run(self, worker.run_id, state)
            if not was_cancelled and state.get('status') == 'completed':
                self._fold_finished_run(worker)
            self._on_finished()
            if was_cancelled:
                # A cancelled stream has no atomic server-side "done" save.
                # Persist the partial visible state only after cancellation has
                # released the core's per-session lock.
                self._sync_session_to_disk()
            from ui.views.chat_resume import refresh_controls
            refresh_controls(self)
        elif run_key == self._current_run_key():
            self.current_worker = None
            self._set_send_button_running(False)
            from ui.qt_deferred import defer
            defer(100, self, lambda key=run_key:
                  self.open_session(*key, force=True) if self._current_run_key() == key else None)
        if not bool(getattr(worker, "was_cancelled", False)):
            self._apply_task_status_check_result(worker)
        event_bus.sessions_updated.emit()

    def _on_run_error(self, run_key, worker, error):
        if self._active_runs.get(run_key) is not worker:
            return
        if run_key in self._session_cache:
            self._session_cache[run_key]['stale'] = True
        self._active_runs.pop(run_key, None)
        self._update_generation_status('stopped' if worker.was_cancelled or not core_process.is_running() else 'error', run_key)
        was_visible = run_key == self._current_run_key()
        self._detached_runs.discard(run_key)
        if was_visible:
            target = getattr(self, '_resume_target', None) or self.active_assistant_turn or self.active_thought_turn
            if isinstance(target, ThoughtMessageTurn) and not target.total_thought_text and not target.tools_count:
                self.turns_layout.removeWidget(target)
                target.deleteLater()
                self.active_thought_turn = None
                target = None
            from ui.views.chat_resume import mark_run
            state = worker.result_state or {'status': 'interrupted', 'error': error}
            if target is None:
                target = self._create_assistant_turn()
                target._pause_placeholder = True
                target.md_view.set_markdown(tr('ui.caption.cebe14d52646'))
            mark_run(self, worker.run_id, state)
            self.current_worker = None
            self._on_error(error)
            if str(error).startswith('CONTINUATION_UNSUPPORTED::'):
                from ui.qt_deferred import defer
                defer(0, self, lambda t=target: self._show_continuation_fallback(t))
        event_bus.sessions_updated.emit()

    def _stop_current_run(self):
        if self.current_worker and self.current_worker.isRunning():
            self.current_worker.cancel()
            self.stop_run_btn.setEnabled(False)
            self.send_btn.setEnabled(False)

    def _refresh_send_shortcut_caption(self, *_):
        if getattr(self, '_send_button_running', False) or getattr(self, '_preparing_attachments', False):
            return
        from ui.chat_hotkeys import send_label
        self.send_btn.setText(send_label())

    def _set_send_button_running(self, running: bool):
        self._send_button_running = bool(running)
        if running:
            self._refresh_stop_thinking_button()
        else:
            self._thinking_control_pending = None
            bind_caption(self.stop_thinking_btn, '')
            self.stop_thinking_btn.setEnabled(False)
        self.send_btn.setEnabled(not self._session_loading and not self._preparing_attachments)
        self.stop_run_btn.setEnabled(True)
        self.stop_run_btn.setVisible(running)
        if running:
            bind_caption(self.send_btn, tr('ui.chat_view.update_task_73d3'))
        else:
            self._refresh_send_shortcut_caption()
        self.send_btn.setObjectName('ChatStudioSendBtn')
        self.send_btn.style().unpolish(self.send_btn)
        self.send_btn.style().polish(self.send_btn)

    def _finish_generation(self):
        if self.active_assistant_turn is not None:
            self.active_assistant_turn.md_view.flush_pending_markdown()
        if self.active_thought_turn is not None:
            self.active_thought_turn.finish_thinking()
        self._set_send_button_running(False)

        self.current_worker = None
        self.active_thought_turn = None
        self.active_assistant_turn = None
        self._resume_target = None
        self._pending_assistant_identity = None
        self._pending_assistant_prefix = ""
        self._generation_insert_index = None
        self._retry_mode_active = False

    def _add_system_message(self, text: str):
        label = bind_caption(QLabel('', self.turns_container), f"⚠️ {text}")
        label.setObjectName("SystemMessageStudioLabel")
        label.setAlignment(Qt.AlignCenter)
        label.setWordWrap(True)
        count = self.turns_layout.count()
        self.turns_layout.insertWidget(count - 1, label)

    def _retranslate(self):
        bind_caption(self.input_edit, tr("chat_placeholder", "Задайте вопрос, отправьте задачу или код..."), setter='setPlaceholderText')
        if not (self.current_worker and self.current_worker.isRunning()):
            set_text(self.send_btn, 'ui.chat_view.ctrl_enter_31af')

    def shutdown_runs(self):
        self.input_state.stash()
        self._delete_temporary_attachment_sources(self._pending_attachments)
        self._pending_attachments.clear()
        workers = list(dict.fromkeys(self._active_runs.values()))
        self._closing_workers.extend(w for w in workers if w not in self._closing_workers)
        for key in list(self._active_runs):
            self._update_generation_status('stopped', key)
        for worker in workers:
            if worker.isRunning():
                worker.cancel(preserve_pause=True)
        for worker in workers:
            if worker.isRunning():
                worker.wait(1500)
        self._active_runs.clear()
        self._detached_runs.clear()
        self.input_state.flush()

    def wait_for_runs(self):
        for worker in self._closing_workers:
            if isValid(worker) and worker.isRunning():worker.wait()
        self._closing_workers.clear()

# =========================================================================
# МЕХАНИКА ТЕГОВ (TAGS)
# =========================================================================

from core.paths import APP_PATHS
TAGS_FILE_PATH = APP_PATHS.root / "ui" / "tags.json"


class SharedTagsState(QObject):
    """Единое состояние тегов для Chat, File Editor и будущих рабочих областей."""

    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.all_tags: List[str] = []
        self.active_tags: List[str] = []
        self._load_tags()

    def _load_tags(self):
        defaults = ["Discussion", "Detailed answer", "Concise"]
        if not TAGS_FILE_PATH.exists():
            self.all_tags = list(defaults)
            self._save_tags_to_file()
            return
        try:
            with open(TAGS_FILE_PATH, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            self.all_tags = [str(tag) for tag in loaded if str(tag).strip()]
            legacy = ["Обсуждение", "Объемный ответ", "Кратко"]
            if self.all_tags in [legacy, legacy + ["Code"], legacy + ["Код"], defaults + ["Code"]]:
                self.all_tags = list(defaults)
                self._save_tags_to_file()
        except Exception:
            self.all_tags = list(defaults)

    def _save_tags_to_file(self):
        try:
            TAGS_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(TAGS_FILE_PATH, "w", encoding="utf-8") as f:
                json.dump(self.all_tags, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[Tags] Could not save tags.json: {e}")

    def set_active_tags(self, tags: List[str]):
        new_active = [tag for tag in tags if tag in self.all_tags]
        if new_active == self.active_tags:
            return
        self.active_tags = new_active
        self.changed.emit()

    def set_tag_active(self, tag_name: str, active: bool):
        changed = False
        if active and tag_name in self.all_tags and tag_name not in self.active_tags:
            self.active_tags.append(tag_name)
            changed = True
        elif not active and tag_name in self.active_tags:
            self.active_tags.remove(tag_name)
            changed = True
        if changed:
            self.changed.emit()

    def delete_tag(self, tag_name: str):
        changed = False
        if tag_name in self.all_tags:
            self.all_tags.remove(tag_name)
            self._save_tags_to_file()
            changed = True
        if tag_name in self.active_tags:
            self.active_tags.remove(tag_name)
            changed = True
        if changed:
            self.changed.emit()

    def rename_tag(self, old_name: str, new_name: str):
        if old_name not in self.all_tags:
            return
        idx = self.all_tags.index(old_name)
        self.all_tags[idx] = new_name
        self._save_tags_to_file()
        if old_name in self.active_tags:
            active_idx = self.active_tags.index(old_name)
            self.active_tags[active_idx] = new_name
        self.changed.emit()

    def add_new_tag(self) -> str:
        base_name = "Новый тег"
        candidate = base_name
        count = 1
        while candidate in self.all_tags:
            count += 1
            candidate = f"{base_name} {count}"
        self.all_tags.append(candidate)
        self._save_tags_to_file()
        self.changed.emit()
        return candidate


shared_tags_state = SharedTagsState()


class TagBadgeWidget(QFrame):
    """Read-only copy of the active tag chip used in the selector panel."""
    removed = Signal(str)

    def __init__(self, text: str, parent=None, *, removable=False):
        super().__init__(parent)
        self.removable = removable
        self.setObjectName("UserMessageTagBadge")
        apply_style(self, '\n            QFrame#UserMessageTagBadge {\n                background-color: @background.surface@;\n                border: 1px solid @accent.primary@;\n                border-radius: 8px;\n            }\n        ')
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        layout.setSpacing(4)

        label = bind_caption(QLabel('', self), text)
        label.setTextFormat(Qt.PlainText)
        apply_style(label, "color:@text.primary@; font-size:11px; font-weight:bold; background:transparent;")
        label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(label)
        self.remove_btn = QToolButton(self)
        symbol_button(self.remove_btn, '×')
        set_text(self.remove_btn, 'ui.chat_view.remove_tag_from_message_ca8a', setter='setToolTip')
        self.remove_btn.setFixedSize(16, 16)
        self.remove_btn.setCursor(Qt.PointingHandCursor)
        apply_style(self.remove_btn, 'QToolButton {background:transparent; border:none; padding:0; color:@text.secondary@; font-size:15px;} QToolButton:hover {color:@text.primary@;}')
        policy = self.remove_btn.sizePolicy()
        policy.setRetainSizeWhenHidden(removable)
        self.remove_btn.setSizePolicy(policy)
        self.remove_btn.clicked.connect(lambda: self.removed.emit(text))
        layout.addWidget(self.remove_btn)
        self.remove_btn.hide()

    def enterEvent(self, event):
        self.remove_btn.setVisible(self.removable)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.remove_btn.hide()
        super().leaveEvent(event)


class TagChipWidget(QFrame):
    """Интерактивный чип тега на панели управления (над полем ввода)."""
    toggled = Signal(str, bool)
    deleted = Signal(str)
    renamed = Signal(str, str)
    geometry_changed = Signal()

    def __init__(self, text: str, is_active: bool = False, parent=None):
        super().__init__(parent)
        self.setObjectName("TagChipFrame")
        self.tag_text = text
        self.is_active = is_active
        self.is_editing = False
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(8, 4, 8, 4)
        self.layout.setSpacing(6)

        self.label = bind_caption(QLabel('', self), text)
        # Пропускаем клики сквозь метку к контейнеру, чтобы клик по тексту срабатывал 100%
        self.label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.label.setStyleSheet("font-size:11px; background: transparent;")

        self.del_btn = QToolButton(self)
        self.del_btn.setObjectName("TagChipDelBtn")
        set_icon(self.del_btn, 'close', size=12)
        self.del_btn.setFixedSize(18, 18)
        self.del_btn.setStyleSheet('QToolButton#TagChipDelBtn {padding:0;margin:0;min-width:18px;min-height:18px;max-width:18px;max-height:18px;border:0;background:transparent;}')
        self.del_btn.setCursor(Qt.PointingHandCursor)
        self.del_btn.clicked.connect(self._on_delete_clicked)
        self.del_btn.hide()

        self.editor = QLineEdit(text, self)
        self.editor.setObjectName("TagChipEditor")
        self.editor.hide()
        self.editor.editingFinished.connect(self._on_rename_finished)

        self.layout.addWidget(self.label)
        self.layout.addWidget(self.editor)

        self._update_style()

    def enterEvent(self, event):
        super().enterEvent(event)
        if not self.is_editing:
            self._show_delete_button(True)

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self._show_delete_button(False)

    def _show_delete_button(self, visible):
        # The button is an overlay inside the chip. Reserve its space only
        # while hovered, so the label stays compact the rest of the time.
        self.layout.setContentsMargins(8, 4, 32 if visible else 8, 4)
        self.del_btn.setVisible(visible)
        self._position_delete_button()
        self.updateGeometry()
        self.geometry_changed.emit()

    def _position_delete_button(self):
        self.del_btn.move(max(0, self.width() - 8 - self.del_btn.width()),
                          max(0, (self.height() - self.del_btn.height()) // 2))
        self.del_btn.raise_()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'del_btn'):
            self._position_delete_button()

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton and not self.is_editing:
            self.is_active = not self.is_active
            self._update_style()
            self.toggled.emit(self.tag_text, self.is_active)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent):
        if event.button() == Qt.LeftButton:
            self._start_edit()
        super().mouseDoubleClickEvent(event)

    def _start_edit(self):
        if self.is_editing:
            return
        self.is_editing = True
        self.label.hide()
        self._show_delete_button(False)
        bind_caption(self.editor, self.tag_text)
        self.editor.show()
        self.editor.setFocus()
        self.editor.selectAll()

        # Switching from the compact label to QLineEdit can make this chip
        # wider and move it to the next FlowLayout row. Notify the owning
        # TagsBar immediately so its popup grows before the user starts typing.
        self.updateGeometry()
        self.geometry_changed.emit()
        QTimer.singleShot(0, self.geometry_changed.emit)

        # Many Toshy widgets deliberately do not take keyboard focus. Relying
        # on QLineEdit.editingFinished therefore misses clicks on those
        # widgets. While a tag is being edited, watch application mouse events
        # and commit on every click outside this chip.
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched, event):
        if self.is_editing:
            if event.type() == QEvent.MouseButtonPress:
                try:
                    global_pos = event.globalPosition().toPoint()
                    clicked_inside = self.rect().contains(self.mapFromGlobal(global_pos))
                except Exception:
                    clicked_inside = False
                if not clicked_inside:
                    self._on_rename_finished()
            elif event.type() == QEvent.ApplicationDeactivate:
                self._on_rename_finished()
        return super().eventFilter(watched, event)

    def _on_rename_finished(self):
        if not self.is_editing:
            return

        new_text = self.editor.text().strip()
        self.is_editing = False

        app = QApplication.instance()
        if app is not None:
            app.removeEventFilter(self)

        self.editor.hide()
        self.label.show()
        self.updateGeometry()
        self.geometry_changed.emit()
        QTimer.singleShot(0, self.geometry_changed.emit)

        # Empty tag names are not useful; in that one case retain the previous
        # value. Any non-empty text present at the moment of the outside click
        # is committed immediately.
        if new_text and new_text != self.tag_text:
            old = self.tag_text
            self.tag_text = new_text
            bind_caption(self.label, new_text)
            self.renamed.emit(old, new_text)

    def _on_delete_clicked(self):
        self.deleted.emit(self.tag_text)

    def _update_style(self):
        if self.is_active:
            apply_style(self, '\n                #TagChipFrame {\n                    background-color: @background.surface@;\n                    border: 1px solid @accent.primary@;\n                    border-radius: 8px;\n                }\n            ')
            apply_style(self.label, "color: @text.primary@; font-weight: bold; background: transparent;")
        else:
            apply_style(self, '\n                #TagChipFrame {\n                    background-color: @background.panel@;\n                    border: 1px solid @border.subtle@;\n                    border-radius: 8px;\n                }\n            ')
            apply_style(self.label, "color: @text.secondary@; background: transparent;")

class FlowLayout(QLayout):
    """Адаптивный лэйаут с переносом элементов на следующие строки (Wrap)."""
    def __init__(self, parent=None, margin=0, h_spacing=6, v_spacing=6):
        super().__init__(parent)
        self.setContentsMargins(margin, margin, margin, margin)
        self._h_spacing = h_spacing
        self._v_spacing = v_spacing
        self._item_list = []

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item):
        self._item_list.append(item)

    def horizontalSpacing(self):
        return self._h_spacing

    def verticalSpacing(self):
        return self._v_spacing

    def count(self):
        return len(self._item_list)

    def itemAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._item_list):
            return self._item_list.pop(index)
        return None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QSize()
        for item in self._item_list:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        size += QSize(margins.left() + margins.right(), margins.top() + margins.bottom())
        return size

    def _do_layout(self, rect, test_only):
        m = self.contentsMargins()
        effective_rect = rect.adjusted(+m.left(), +m.top(), -m.right(), -m.bottom())
        x = effective_rect.x()
        y = effective_rect.y()
        line_height = 0

        for item in self._item_list:
            wid = item.widget()
            space_x = self.horizontalSpacing()
            space_y = self.verticalSpacing()
            next_x = x + item.sizeHint().width() + space_x

            if next_x - space_x > effective_rect.right() and line_height > 0:
                x = effective_rect.x()
                y = y + line_height + space_y
                next_x = x + item.sizeHint().width() + space_x
                line_height = 0

            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), item.sizeHint()))

            x = next_x
            line_height = max(line_height, item.sizeHint().height())

        return y + line_height - rect.y() + m.bottom()

class TagsBarWidget(QFrame):
    """Панель управления тегами с общим состоянием для всех рабочих областей."""
    tag_state_changed = Signal()

    def __init__(self, parent=None, state: Optional[SharedTagsState] = None):
        super().__init__(parent)
        self.setObjectName("TagsBarContainer")
        self._state = state or shared_tags_state

        # Сетка с переносом строк (FlowLayout) — тот же UI, что использует Chat.
        self.flow_layout = FlowLayout(self, margin=6, h_spacing=6, v_spacing=6)

        self.add_btn = QToolButton(self)
        self.add_btn.setObjectName("TagAddBtn")
        symbol_button(self.add_btn, "+")
        set_text(self.add_btn, 'ui.chat_view.add_new_tag_411e', setter='setToolTip')
        self.add_btn.setFixedSize(24, 24)
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._add_new_tag)

        self._state.changed.connect(self._on_shared_state_changed)
        self._rebuild_chips()

    @property
    def all_tags(self) -> List[str]:
        return self._state.all_tags

    @property
    def active_tags(self) -> List[str]:
        return self._state.active_tags

    def _on_shared_state_changed(self):
        self._rebuild_chips()
        self.tag_state_changed.emit()

    def _rebuild_chips(self, edit_tag: Optional[str] = None):
        for i in reversed(range(self.flow_layout.count())):
            item = self.flow_layout.itemAt(i)
            w = item.widget() if item else None
            if w and w != self.add_btn:
                self.flow_layout.removeWidget(w)
                w.deleteLater()

        for tag in self.all_tags:
            is_on = tag in self.active_tags
            chip = TagChipWidget(tag, is_active=is_on, parent=self)
            chip.toggled.connect(self._on_chip_toggled)
            chip.deleted.connect(self._on_chip_deleted)
            chip.renamed.connect(self._on_chip_renamed)
            chip.geometry_changed.connect(self._on_chip_geometry_changed)
            if edit_tag and tag == edit_tag:
                QTimer.singleShot(0, chip._start_edit)

            idx = self.flow_layout.indexOf(self.add_btn)
            if idx >= 0:
                self.flow_layout.takeAt(idx)
                self.flow_layout.addWidget(chip)
                self.flow_layout.addWidget(self.add_btn)
            else:
                self.flow_layout.addWidget(chip)

        if self.flow_layout.indexOf(self.add_btn) == -1:
            self.flow_layout.addWidget(self.add_btn)

    def _on_chip_geometry_changed(self):
        # FlowLayout has height-for-width. A chip entering/leaving edit mode can
        # change its sizeHint and therefore the number of wrapped rows without
        # changing tag state. Force both Chat and File Editor hosts to recompute
        # the popup height immediately instead of waiting for Enter.
        self.flow_layout.invalidate()
        self.updateGeometry()
        self.tag_state_changed.emit()

    def _on_chip_toggled(self, tag_name: str, active: bool):
        self._state.set_tag_active(tag_name, active)

    def _on_chip_deleted(self, tag_name: str):
        self._state.delete_tag(tag_name)

    def _on_chip_renamed(self, old_name: str, new_name: str):
        self._state.rename_tag(old_name, new_name)

    def _add_new_tag(self):
        candidate = self._state.add_new_tag()
        # Остальные панели уже синхронизировались через state.changed; только
        # здесь сразу переходим в редактирование нового чипа, как и раньше.
        self._rebuild_chips(edit_tag=candidate)

    def get_active_tags(self) -> List[str]:
        return list(self.active_tags)

    def set_active_tags(self, tags: List[str]):
        self._state.set_active_tags(tags)

    def toggle_panel(self):
        self.setVisible(not self.isVisible())

class GradientFadeOverlay(QWidget):
    """Мягкое градиентное затемнение сверху и снизу ленты чата (Google AI Studio style)."""
    def __init__(self, scroll_area: QScrollArea, fade_height: int = 32, fade_color: str = None):
        super().__init__(scroll_area)
        self.scroll_area = scroll_area
        self.fade_height = fade_height
        from ui.themes.manager import theme_manager
        self.fade_color = theme_manager.color('workspace.background')
        theme_manager.theme_changed.connect(self._theme_changed)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        v_bar = self.scroll_area.verticalScrollBar()
        v_bar.valueChanged.connect(lambda _: self.update())
        v_bar.rangeChanged.connect(lambda _min, _max: self.update())

    def _theme_changed(self, *_):
        from ui.themes.manager import theme_manager
        self.fade_color = theme_manager.color('workspace.background')
        self.update()

    def paintEvent(self, event):
        v_bar = self.scroll_area.verticalScrollBar()
        val = v_bar.value()
        max_val = v_bar.maximum()

        # Плавное появление затемнения при отрыве от верхнего/нижнего края (за 25px скролла)
        top_alpha = min(1.0, val / 25.0) if max_val > 0 else 0.0
        bot_alpha = min(1.0, (max_val - val) / 25.0) if max_val > 0 else 0.0

        if top_alpha <= 0 and bot_alpha <= 0:
            return

        painter = QPainter(self)
        w = self.width()
        h = self.height()
        fh = min(self.fade_height, h // 4)

        if fh <= 0:
            return

        # Верхнее мягкое затемнение
        if top_alpha > 0:
            c_top = QColor(self.fade_color)
            c_top.setAlpha(int(255 * top_alpha))
            c_trans = QColor(self.fade_color)
            c_trans.setAlpha(0)

            top_grad = QLinearGradient(0, 0, 0, fh)
            top_grad.setColorAt(0.0, c_top)
            top_grad.setColorAt(1.0, c_trans)
            painter.fillRect(0, 0, w, fh, top_grad)

        # Нижнее мягкое затемнение
        if bot_alpha > 0:
            c_bot = QColor(self.fade_color)
            c_bot.setAlpha(int(255 * bot_alpha))
            c_trans = QColor(self.fade_color)
            c_trans.setAlpha(0)

            bot_grad = QLinearGradient(0, h - fh, 0, h)
            bot_grad.setColorAt(0.0, c_trans)
            bot_grad.setColorAt(1.0, c_bot)
            painter.fillRect(0, h - fh, w, fh, bot_grad)


class ChatScrollArea(QScrollArea):
    """Область прокрутки чата с автоматическим позиционированием мягкого оверлея."""
    older_requested = Signal()
    navigation_started = Signal()
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("ChatStudioScrollArea")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.fade_overlay = GradientFadeOverlay(self, fade_height=32)
        from .chat_middle_scroll import MiddleScroll
        self.middle_scroll = MiddleScroll(self)

    def wheelEvent(self, event):
        if event.angleDelta().y() or event.pixelDelta().y():
            self.navigation_started.emit()
        super().wheelEvent(event)
        if (event.angleDelta().y() > 0 or event.pixelDelta().y() > 0) and self.verticalScrollBar().value() <= 40:
            self.older_requested.emit()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End):
            self.navigation_started.emit()
        super().keyPressEvent(event)
        if event.key() in (Qt.Key_Up, Qt.Key_PageUp, Qt.Key_Home) and self.verticalScrollBar().value() <= 40:
            self.older_requested.emit()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.fade_overlay.setGeometry(0, 0, self.width(), self.height())
        self.fade_overlay.raise_()
