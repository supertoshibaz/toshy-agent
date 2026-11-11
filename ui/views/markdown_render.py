"""Small dependency-free Markdown -> Qt rich-text renderer.

Used where we need a fully rendered, read-only representation (file preview and
unselected canvas cards). It intentionally covers the Markdown subset exposed by
Toshy's editor toolbar plus GFM-style tables.
"""
from __future__ import annotations
from ui.i18n import bind_caption
from ui.styles.binding import apply_style, resolve, apply_rich_text
from ui.themes.manager import theme_manager
from PySide6.QtGui import QTextCursor

import html
import re
from .markdown_anchors import strip_block_id
from typing import List



def _escape_text(value, quote=True):
    # User text may itself describe design tokens; only generated CSS resolves them.
    return html.escape(value, quote=quote).replace('@', '&#64;')


def _split_table_row(row: str) -> List[str]:
    value = row.strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|"):
        value = value[:-1]
    return [cell.strip() for cell in value.split("|")]


def _is_table_separator(row: str) -> bool:
    cells = _split_table_row(row)
    return bool(cells) and all(re.fullmatch(r":?-{2,}:?", cell) for cell in cells)


def _format_inline(text: str) -> str:
    # Escape raw HTML first. Markdown inserted by the user is then converted to
    # the small HTML subset QTextDocument understands.
    value = _escape_text(text, quote=True)
    leading = re.match(r"^[ \t]+", value)
    if leading:
        value = "&#160;" * len(leading.group().expandtabs(4)) + value[leading.end():]
    value = re.sub(r"`([^`]+)`", r"<code style='background:@background.panel@; color:@text.primary@;'>\1</code>", value)
    value = re.sub(r"\*\*\*(.+?)\*\*\*", r"<b><i style='color:@text.primary@;'>\1</i></b>", value)
    value = re.sub(r"\*\*(.+?)\*\*", r"<b style='color:@text.secondary@;'>\1</b>", value)
    value = re.sub(r"~~(.+?)~~", r"<s style='color:@text.secondary@;'>\1</s>", value)
    value = re.sub(r"(?<!\*)\*([^*]+?)\*(?!\*)", r"<i style='color:@text.secondary@;'>\1</i>", value)
    value = re.sub(
        r"\[([^\[\]\n]+)\]\(([^()\[\]\n]+)\)",
        r"<a href='\2' style='color:@text.secondary@; text-decoration:underline;'>\1</a>",
        value,
    )
    # Raw URLs are links too. The negative look-behind avoids URLs already
    # embedded in the href attribute generated above.
    value = re.sub(
        r"(?<!['\">])(https?://[^\s<]+)",
        r"<a href='\1' style='color:@accent.primary@; text-decoration:underline;'>\1</a>",
        value,
    )
    return value


def _render_table(headers: List[str], separators: List[str], rows: List[List[str]]) -> str:
    aligns: List[str] = []
    for cell in separators:
        if cell.startswith(":") and cell.endswith(":"):
            aligns.append("center")
        elif cell.endswith(":"):
            aligns.append("right")
        else:
            aligns.append("left")

    out = [
        "<table cellspacing='0' cellpadding='0' style='border-collapse:collapse; width:100%; margin:0;'>",
        "<tr>",
    ]
    for index, header in enumerate(headers):
        align = aligns[index] if index < len(aligns) else "left"
        out.append(
            f"<th style='border:1px solid @border.subtle@; background:@background.surface@; padding:6px 9px; "
            f"text-align:{align}; color:@text.primary@;'>{_format_inline(header)}</th>"
        )
    out.append("</tr>")

    for row_index, row in enumerate(rows):
        bg = "@background.panel@" if row_index % 2 == 0 else "@background.panel@"
        out.append("<tr>")
        for index in range(len(headers)):
            align = aligns[index] if index < len(aligns) else "left"
            cell = row[index] if index < len(row) else ""
            out.append(
                f"<td style='border:1px solid @border.subtle@; background:{bg}; padding:5px 9px; "
                f"text-align:{align}; color:@text.primary@;'>{_format_inline(cell)}</td>"
            )
        out.append("</tr>")
    out.append("</table>")
    return "".join(out)


def markdown_to_html(markdown: str, *, text_align: str = "left", compact: bool = False,
                     heading_anchors: bool = False, source_line_offset: int = 0, line_alignments=None,
                     editor_headings: bool = False, _theme_template: bool = False) -> str:
    """Render the Markdown subset used by Toshy to Qt-compatible HTML."""
    source = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.split("\n")
    source_index = 0
    class Fragments(list):
        def append(self, fragment):
            alignment = (line_alignments or {}).get(source_line_offset + source_index)
            if alignment in {'left','center','right'}:
                fragment = f"<div align='{alignment}'>" + fragment + '</div>'
            super().append(fragment)
    out = Fragments()
    i = 0

    while i < len(lines):
        source_index = i
        line = lines[i]
        line = strip_block_id(line)
        stripped = line.strip()
        if not stripped and lines[i].strip():
            i += 1
            continue
        line_anchor = f"<a name='toshy-heading-{source_line_offset+i}'></a>" if heading_anchors else ''

        if stripped.startswith("```"):
            language = stripped[3:].strip()
            code_lines: List[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            if i < len(lines):
                i += 1

            # QTextDocument only implements a subset of browser CSS. A one-cell
            # table is considerably more reliable than a styled <div> for the
            # fenced-code card: border/background/padding survive setHtml(), and
            # <pre> keeps whitespace/newlines inside the code body.
            label = (
                "<div style='color:@text.secondary@; font-size:10px; margin:0 0 5px 0;'>"
                + _escape_text(language)
                + "</div>"
                if language
                else ""
            )
            code = _escape_text("\n".join(code_lines))
            if not code:
                code = "&#8203;"
            out.append(
                "<table width='100%' border='1' cellspacing='0' cellpadding='8' "
                "bgcolor='@background.input@' style='border-color:@border.subtle@; margin-top:6px; margin-bottom:6px;'>"
                "<tr><td bgcolor='@background.input@'>"
                + label
                + "<pre style='margin:0; padding:0; color:@text.primary@; "
                  "background-color:@background.input@; font-family:monospace;'>"
                + code
                + "</pre></td></tr></table>"
            )
            continue

        if not stripped:
            # Один пустой абзац занимает одну строку, без дополнительного переноса Qt.
            out.append("<p style='margin:0; padding:0;'>&#8203;</p>")
            i += 1
            continue

        # A table is recognized only when the *next* line is a valid separator.
        # This also prevents table separator rows from being mistaken for <hr>.
        if "|" in line and i + 1 < len(lines) and _is_table_separator(lines[i + 1]):
            headers = _split_table_row(line)
            separators = _split_table_row(lines[i + 1])
            rows: List[List[str]] = []
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(_split_table_row(lines[i]))
                i += 1
            out.append(_render_table(headers, separators, rows))
            continue

        if re.fullmatch(r"(?:---|___|\*\*\*)", stripped):
            out.append("<hr style='border:0; border-top:1px solid @border.subtle@; margin:0;'>")
            i += 1
            continue

        heading = re.match(r"^(#{1,5})(?!#)\s+(.*)$", line)
        if heading:
            level = len(heading.group(1))
            from ui.markdown_style import heading_css
            size = {1: 22, 2: 19, 3: 17, 4: 15, 5: 14}[level]
            style = heading_css(level).replace(theme_manager.value('text.secondary'), '@text.secondary@') if editor_headings else (
                f"font-size:{size}px; "
                "font-weight:600; color:@text.primary@; margin:0;")
            anchor = f"<a name='toshy-heading-{source_line_offset + i}'></a>" if heading_anchors else ""
            out.append(
                f'<div style="{style}">'
                f"{anchor}{_format_inline(heading.group(2))}</div>"
            )
            i += 1
            continue

        quote = re.match(r"^\s*>\s?(.*)$", line)
        if quote:
            out.append(
                "<table cellspacing='0' cellpadding='0' width='100%' style='margin-top:2px;margin-bottom:2px;'>"
                "<tr><td width='3' bgcolor='@accent.primary@'></td><td width='10'></td>"
                "<td style='color:@text.secondary@;'>"
                + line_anchor + _format_inline(quote.group(1))
                + "</td></tr></table>"
            )
            i += 1
            continue

        bullet = re.match(r"^([ \t]*)[-+*]\s+(.*)$", line)
        if bullet:
            margin = 14 + len(bullet.group(1).expandtabs(4)) * 4
            out.append(f"<div style='margin:0 0 0 {margin}px;'>" + line_anchor + '• ' + _format_inline(bullet.group(2)) + "</div>")
            i += 1
            continue

        numbered = re.match(r"^([ \t]*)(\d+[.)])\s+(.*)$", line)
        if numbered:
            out.append(
                f"<div style='margin:0 0 0 {14 + len(numbered.group(1).expandtabs(4)) * 4}px;'>{line_anchor}<b>"
                + _escape_text(numbered.group(2))
                + "</b> "
                + _format_inline(numbered.group(3))
                + "</div>"
            )
            i += 1
            continue

        out.append("<div style='margin:0;'>" + line_anchor + _format_inline(line) + "</div>")
        i += 1

    align = text_align if text_align in {"left", "center", "right"} else "left"
    rendered = (
        "<html><body style='background:transparent; color:@text.primary@; "
        f"text-align:{align}; margin:0; padding:0;'>"
        + "".join(out)
        + "</body></html>"
    )
    return rendered if _theme_template else resolve(rendered)


# -------------------------------------------------------------------------
# Widget renderer
# -------------------------------------------------------------------------
# QTextDocument cannot host a normal interactive QWidget.  For read-only
# Markdown we therefore keep ordinary Markdown in Qt rich text, but split
# fenced code blocks out and render them with the same real widget as chat.

from math import ceil

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QGridLayout,
    QLabel,
    QLayout,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ui.views.code_block import CodeBlockWidget


def _strip_one_trailing_newline(text: str) -> str:
    """Remove only the structural newline before an extracted block."""
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n") or text.endswith("\r"):
        return text[:-1]
    return text


def _line_text(line: str) -> str:
    return line.rstrip("\r\n")


def _parse_fenced_lines(lines: List[str]) -> tuple[str, str]:
    first = _line_text(lines[0]) if lines else "```"
    language = first.strip()[3:].strip() if first.strip().startswith("```") else ""

    body = lines[1:]
    if body and _line_text(body[-1]).strip().startswith("```"):
        body = body[:-1]

    code = _strip_one_trailing_newline("".join(body))
    return language, code


def _parse_table_lines(lines: List[str]) -> tuple[List[str], List[str], List[List[str]]]:
    plain = [_line_text(line) for line in lines]
    headers = _split_table_row(plain[0])
    separators = _split_table_row(plain[1])
    rows = [_split_table_row(line) for line in plain[2:]]
    return headers, separators, rows


def _split_render_parts(markdown: str, *, with_source_lines: bool = False) -> list:
    """Extract only fenced code and GFM tables from the normal text flow.

    Ordinary Markdown remains untouched and is still rendered by the same rich
    text path used for the built-in help/about documents. A single structural
    newline at a native-block boundary is consumed; actual blank source lines
    are preserved.
    """
    source = markdown.replace("\r\n", "\n").replace("\r", "\n")
    lines = source.splitlines(keepends=True)
    from ui.views.chat_markdown import fenced_blocks
    fences = {block.start: block for block in fenced_blocks(source)}

    parts = []
    text_buffer: List[str] = []
    text_start = 0

    def add_part(kind, payload, source_line):
        parts.append((kind, payload, source_line) if with_source_lines else (kind, payload))

    def flush_text(*, before_native_block: bool) -> None:
        if not text_buffer:
            return
        text = "".join(text_buffer)
        text_buffer.clear()
        if before_native_block:
            text = _strip_one_trailing_newline(text)
        if text:
            add_part("text", text, text_start)

    i = 0
    while i < len(lines):
        line = _line_text(lines[i])
        stripped = line.strip()

        from ui.views.inline_images import image_reference
        if image_reference(line):
            flush_text(before_native_block=True)
            add_part("image", line, i)
            i += 1
            continue

        if i in fences:
            block = fences[i]
            end = block.end + 1
            flush_text(before_native_block=True)
            add_part("code", (block.language, block.code), i)
            i = end
            continue

        if (
            "|" in line
            and i + 1 < len(lines)
            and _is_table_separator(_line_text(lines[i + 1]))
        ):
            end = i + 2
            while end < len(lines):
                candidate = _line_text(lines[end])
                if end in fences or not candidate.strip() or "|" not in candidate:
                    break
                end += 1

            flush_text(before_native_block=True)
            add_part("table", _parse_table_lines(lines[i:end]), i)
            i = end
            continue

        if not text_buffer:
            text_start = i
        text_buffer.append(lines[i])
        i += 1

    flush_text(before_native_block=False)
    return parts


class MarkdownTableWidget(QFrame):
    """Native read-only block for a GFM pipe table."""

    def __init__(
        self,
        headers: List[str],
        separators: List[str],
        rows: List[List[str]],
        parent=None,
        *,
        interactive_text: bool = True,
    ):
        super().__init__(parent)
        self.setObjectName("MarkdownTableBlock")
        self.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Minimum)
        apply_style(self, 
            "QFrame#MarkdownTableBlock {"
            " background: @background.input@;"
            " border: 1px solid @border.subtle@;"
            " border-radius: 5px;"
            "}"
        )

        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(0)
        grid.setVerticalSpacing(0)

        aligns = []
        for cell in separators:
            if cell.startswith(":") and cell.endswith(":"):
                aligns.append(Qt.AlignHCenter)
            elif cell.endswith(":"):
                aligns.append(Qt.AlignRight)
            else:
                aligns.append(Qt.AlignLeft)

        column_count = max(
            [len(headers), len(separators), 1] + [len(row) for row in rows]
        )
        normalized_headers = headers + [""] * (column_count - len(headers))
        normalized_rows = [
            row + [""] * (column_count - len(row))
            for row in rows
        ]

        for row_index, row in enumerate([normalized_headers, *normalized_rows]):
            is_header = row_index == 0
            for column_index, value in enumerate(row):
                label = QLabel(self)
                label.setTextFormat(Qt.RichText)
                label.setWordWrap(True)
                apply_rich_text(label, _format_inline(value))
                horizontal = aligns[column_index] if column_index < len(aligns) else Qt.AlignLeft
                label.setAlignment(horizontal | Qt.AlignVCenter)
                if interactive_text:
                    label.setTextInteractionFlags(
                        Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
                    )
                    label.setOpenExternalLinks(False)
                    label.linkActivated.connect(self._open_reference)
                else:
                    label.setTextInteractionFlags(Qt.NoTextInteraction)

                borders = []
                if column_index > 0:
                    borders.append("border-left: 1px solid @border.subtle@;")
                if row_index > 0:
                    borders.append("border-top: 1px solid @border.subtle@;")

                background = "@background.surface@" if is_header else "@background.input@"
                foreground = "@text.primary@" if is_header else "@text.primary@"
                weight = "600" if is_header else "400"
                apply_style(label, 
                    "QLabel {"
                    f" background: {background};"
                    f" color: {foreground};"
                    f" font-weight: {weight};"
                    " padding: 5px 9px;"
                    + "".join(borders)
                    + "}"
                )
                grid.addWidget(label, row_index, column_index)



    def _open_reference(self, reference):
        from .inline_images import open_reference
        open_reference(reference,getattr(self,'base_path',None),switch_view=True)


class _AutoHeightRichText(QTextBrowser):
    """A QTextBrowser chunk whose height follows its document height."""

    def __init__(self, parent=None, *, interactive: bool = True):
        super().__init__(parent)
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(0)
        self._height_timer.timeout.connect(self._sync_height)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.anchorClicked.connect(self._open_reference)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        apply_style(self, 
            "QTextBrowser { background: transparent; color: @text.primary@; "
            "border: none; padding: 0; }"
        )
        theme_manager.theme_changed.connect(self._refresh_theme)
        self.document().setDocumentMargin(0)
        if interactive:
            self.setTextInteractionFlags(
                Qt.TextSelectableByMouse | Qt.LinksAccessibleByMouse
            )
        else:
            self.setTextInteractionFlags(Qt.NoTextInteraction)

    def _refresh_theme(self, *_):
        if not hasattr(self, '_theme_html'):
            return
        cursor = self.textCursor()
        anchor, position = cursor.anchor(), cursor.position()
        super().setHtml(resolve(self._theme_html))
        cursor = self.textCursor()
        last = self.document().characterCount()-1
        cursor.setPosition(min(anchor, last))
        cursor.setPosition(min(position, last), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self._height_timer.start()

    def _open_reference(self, url):
        from .inline_images import open_reference
        open_reference(url.toString(),getattr(self,'base_path',None),switch_view=True)

    def setHtml(self, text: str):
        self._theme_html = text
        super().setHtml(resolve(text))
        self._height_timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_height()

    def _sync_height(self):
        self._height_timer.stop()
        width = max(1, self.viewport().width())
        self.document().setTextWidth(width)
        height = max(1, int(ceil(self.document().size().height())))
        if self.height() != height:
            self.setFixedHeight(height)

    def wheelEvent(self, event):
        # This child never scrolls by itself. Let the containing Markdown view
        # consume the wheel instead of trapping it inside a text fragment.
        event.ignore()


class MarkdownRenderView(QScrollArea):
    """Rendered Markdown view with real, shared fenced-code widgets.

    Ordinary Markdown stays in QTextDocument chunks. Fenced code uses the exact
    same CodeBlockWidget as chat, while GFM pipe tables use a native table block.
    Only those structures leave the normal help/about-style text flow.
    """

    image_removed = Signal(str)

    def __init__(
        self,
        parent=None,
        *,
        compact: bool = False,
        text_align: str = "left",
        interactive_text: bool = True,
        vertical_scrollbar: bool = True,
        track_headings: bool = False,
    ):
        super().__init__(parent)
        self.base_path = None
        self.images_removable = False
        self._image_chunks = []
        self._markdown = ""
        self._compact = bool(compact)
        self._text_align = text_align
        self._interactive_text = bool(interactive_text)
        self._track_headings = bool(track_headings)
        self._document_margin = 0
        self._text_chunks: list[_AutoHeightRichText] = []
        self._last_context_widget = None
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(0)
        self._height_timer.timeout.connect(self._refresh_chunk_heights)

        self.setFrameShape(QFrame.NoFrame)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(
            Qt.ScrollBarAlwaysOn if vertical_scrollbar else Qt.ScrollBarAlwaysOff
        )
        apply_style(self, 
            "QScrollArea { background: transparent; border: none; padding: 0; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
        )

        self._content = QWidget(self)
        self._content.setObjectName("MarkdownRenderContent")
        self._content.setStyleSheet("QWidget#MarkdownRenderContent { background: transparent; }")
        self._layout = QVBoxLayout(self._content)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self._layout.setAlignment(Qt.AlignTop)
        self._layout.setSizeConstraint(QLayout.SetMinAndMaxSize)
        self.setWidget(self._content)
        from ui.views.link_cursor import register_link_cursor
        register_link_cursor(self.viewport(), self._link_cursor_at_global)

    def setDocumentMargin(self, margin: float):
        self._document_margin = max(0, int(round(margin)))
        m = self._document_margin
        self._layout.setContentsMargins(m, m, m, m)

    def set_markdown(
        self,
        markdown: str,
        *,
        text_align: str | None = None,
        compact: bool | None = None,
    ):
        alignment_changed = text_align is not None and text_align != self._text_align
        if text_align is not None:
            self._text_align = text_align
        if compact is not None:
            self._compact = bool(compact)
        if self._text_align == 'card_center':
            self._content.setMaximumSize(16777215, 16777215)
            self._layout.setSizeConstraint(QLayout.SetMinimumSize)
        else:
            self._layout.setSizeConstraint(QLayout.SetMinAndMaxSize)

        # Alignment/compactness changes still require a rebuild even if source
        # text itself did not change, so keep this intentionally unconditional.
        self._markdown = markdown
        self._clear_blocks()
        if self._text_align == "card_center":
            self._layout.addStretch(1)

        for part in _split_render_parts(markdown, with_source_lines=self._track_headings):
            self._append_part(part)

        if self._text_align == "card_center":
            self._layout.addStretch(1)
        if alignment_changed or self._text_align == 'card_center':
            self._refresh_chunk_heights()
        else:
            self._height_timer.start()

    def _append_part(self, part):
        kind, payload = part[:2]
        source_line = part[2] if self._track_headings else 0
        if kind == 'image':
            from ui.views.inline_images import InlineImageWidget, image_reference, resolve_image, image_size_mode
            reference = image_reference(payload)
            widget = InlineImageWidget(resolve_image(reference[1], self.base_path), self._content,
                removable=self.images_removable, line_height=self.fontMetrics().lineSpacing())
            widget.size_mode = image_size_mode(payload)
            widget.size_changed.connect(lambda _:self._height_timer.start())
            widget.activated.connect(lambda w=widget:[other.set_selected(other is w) for other in self._image_chunks])
            widget.remove_requested.connect(lambda source=payload: self.image_removed.emit(source))
            self._image_chunks.append(widget)
            self._layout.addWidget(widget, 0, Qt.AlignHCenter if self._text_align in {'center', 'card_center'} else Qt.AlignRight if self._text_align == 'right' else Qt.AlignLeft)
            return widget

        if kind == "code":
            language, code = payload
            block = CodeBlockWidget(code, language=language, parent=self._content)
            block.presentation_changed.connect(lambda: self._height_timer.start())
            block.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self._relay_context_menu(block)
            self._relay_context_menu(block.code_edit)
            self._layout.addWidget(block)
            return block

        if kind == "table":
            headers, separators, rows = payload
            table = MarkdownTableWidget(
                headers,
                separators,
                rows,
                parent=self._content,
                interactive_text=self._interactive_text,
            )
            table.base_path = self.base_path
            self._relay_context_menu(table)
            self._layout.addWidget(table, 0, Qt.AlignLeft)
            return table

        rich = _AutoHeightRichText(
            self._content,
            interactive=self._interactive_text,
        )
        rich.setFont(self.font())
        rich.base_path = self.base_path
        rich.document().setDefaultFont(self.font())
        rich.setHtml(
            markdown_to_html(
                payload,
                text_align="center" if self._text_align == "card_center" else self._text_align,
                compact=self._compact,
                heading_anchors=self._track_headings,
                source_line_offset=source_line,
                line_alignments=getattr(self, 'line_alignments', None),
                editor_headings=True,
                _theme_template=True,
            )
        )
        self._relay_context_menu(rich)
        self._text_chunks.append(rich)
        from ui.views.link_cursor import register_link_cursor
        register_link_cursor(rich.viewport(), self._link_cursor_at_global)
        self._layout.addWidget(rich)
        return rich

    def link_at_global(self, pos):
        for chunk in self._text_chunks:
            local = chunk.viewport().mapFromGlobal(pos)
            if chunk.viewport().rect().contains(local):
                reference = chunk.anchorAt(local)
                if reference:
                    return reference
        return None

    def _link_cursor_at_global(self, pos):
        reference = self.link_at_global(pos)
        targets = [chunk.viewport() for chunk in self._text_chunks
                   if chunk.viewport().rect().contains(chunk.viewport().mapFromGlobal(pos))]
        return (reference, targets) if reference else None

    def setFont(self, font):
        super().setFont(font)
        for chunk in getattr(self, "_text_chunks", []):
            chunk.setFont(font)
            chunk.document().setDefaultFont(font)
            chunk._sync_height()

    def _clear_blocks(self):
        self._text_chunks.clear()
        self._image_chunks.clear()
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()

    def _refresh_chunk_heights(self):
        self._height_timer.stop()
        for index in range(self._layout.count()):
            widget = self._layout.itemAt(index).widget()
            if widget is not None:
                widget.show()
        # Resolve the real viewport width before measuring newly built text
        # chunks. Otherwise their initial 100px geometry is briefly painted.
        self._content.resize(self.viewport().width(), max(self.viewport().height(), self._content.minimumHeight()))
        self._layout.invalidate()
        self._layout.activate()
        for chunk in self._text_chunks:
            chunk._sync_height()
        available = max(1, self.viewport().width()-2*self._document_margin)
        for widget in self._image_chunks:
            size = widget.extent(available)
            widget.setFixedSize(round(size.width()), round(size.height()))
        self._layout.setAlignment(Qt.AlignmentFlag(0) if self._text_align == 'card_center' else Qt.AlignTop)
        self._content.setMinimumHeight(max(0, self._layout.sizeHint().height()))
        self._content.resize(self.viewport().width(), max(self.viewport().height(), self._content.minimumHeight()))
        self._layout.invalidate()
        self._layout.activate()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._height_timer.start()

    def _relay_context_menu(self, widget: QWidget):
        widget.setContextMenuPolicy(Qt.CustomContextMenu)

        def relay(pos, source=widget):
            self._last_context_widget = source
            global_pos = source.mapToGlobal(pos)
            local_pos = self.mapFromGlobal(global_pos)
            self.customContextMenuRequested.emit(local_pos)

        widget.customContextMenuRequested.connect(relay)

    def createStandardContextMenu(self) -> QMenu:
        source = self._last_context_widget
        if source is not None and hasattr(source, "createStandardContextMenu"):
            return source.createStandardContextMenu()

        focus = QApplication.focusWidget()
        while focus is not None and focus is not self:
            if hasattr(focus, "createStandardContextMenu"):
                return focus.createStandardContextMenu()
            focus = focus.parentWidget()

        return QMenu(self)
