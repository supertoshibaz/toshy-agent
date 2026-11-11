from ui.themes.manager import theme_manager
import re
from ui.markdown_style import HEADING_POINTS, HEADING_COLOR
from ui.views.markdown_anchors import BLOCK_ID
from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import (
    QSyntaxHighlighter, QTextCharFormat, QColor, QFont
)


class ObsidianLivePreviewHighlighter(QSyntaxHighlighter):
    """Вынесенный хайлайтер Markdown Live Preview."""
    def __init__(self, parent_doc, editor):
        super().__init__(parent_doc)
        self.editor = editor
        theme_manager.theme_changed.connect(self._theme_changed)
        editor.live_preview_highlighter = self

        self.has_selection = False
        self.sel_start = -1
        self.sel_end = -1
        self.active_block_num = 0
        self._active_bounds = None
        self._active_state_initialized = False
        self._dirty_neighbors = []

        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(15)
        self._sync_timer.timeout.connect(self._sync_and_rehighlight)

        self.editor.cursorPositionChanged.connect(self._schedule_sync)
        self.editor.selectionChanged.connect(self._schedule_sync)
        self.editor.installEventFilter(self)
        parent_doc.contentsChange.connect(self._contents_changed)
        self._schedule_sync()

    def _theme_changed(self, *_):
        self.rehighlight()
        self.editor.viewport().update()

    def _contents_changed(self, position, removed, added):
        if removed or added:
            doc = self.document()
            if doc is None: return
            first = doc.findBlock(min(position, doc.characterCount() - 1))
            previous = first.previous()
            if previous.isValid(): self._dirty_neighbors.append(previous)
            last = doc.findBlock(min(position + added, doc.characterCount() - 1))
            block = first
            while block.isValid() and block.blockNumber() <= last.blockNumber():
                self._dirty_neighbors.append(block)
                block = block.next()
            self._schedule_sync()

    def eventFilter(self, watched, event):
        if watched is self.editor and event.type() in (QEvent.FocusIn, QEvent.FocusOut, QEvent.Show):
            self._dirty_neighbors.append(self.editor.textCursor().block())
            self._schedule_sync()
            self.editor.viewport().update()
        return False

    def _schedule_sync(self):
        if not self._sync_timer.isActive():
            self._sync_timer.start()

    def _sync_and_rehighlight(self):
        self._sync_timer.stop()
        is_focused = self.editor.hasFocus()
        cursor = self.editor.textCursor()
        old_bounds = self._active_bounds

        # Если редактор не в фокусе — ни одна строка не активна, все символы разметки скрываются
        if not is_focused:
            self.has_selection = False
            self.sel_start = -1
            self.sel_end = -1
            self.active_block_num = -1
        elif cursor.hasSelection():
            self.has_selection = True
            self.sel_start = cursor.selectionStart()
            self.sel_end = cursor.selectionEnd()
            self.active_block_num = cursor.blockNumber()
        else:
            self.has_selection = False
            self.sel_start = -1
            self.sel_end = -1
            self.active_block_num = cursor.blockNumber()

        doc = self.document()
        if doc is None:
            return
        if not is_focused:
            new_bounds = None
        elif cursor.hasSelection():
            new_bounds = (doc.findBlock(cursor.selectionStart()),
                          doc.findBlock(max(cursor.selectionStart(), cursor.selectionEnd() - 1)))
        else:
            new_bounds = (cursor.block(), cursor.block())
        self._active_bounds = new_bounds
        if not self._active_state_initialized:
            # Initial formatting covers the whole document once. Later cursor
            # gestures only change whether the old/new blocks expose markup.
            self._active_state_initialized = True
            self.rehighlight()
            self._dirty_neighbors.clear()
            return

        def interval(bounds):
            if bounds is None or not all(b.isValid() for b in bounds):
                return None
            return bounds[0].blockNumber(), bounds[1].blockNumber()

        old, new = interval(old_bounds), interval(new_bounds)
        if len(self._dirty_neighbors) > 256:
            # A bulk replacement is a real document-wide change. One native
            # pass is cheaper than thousands of rehighlightBlock calls.
            self._dirty_neighbors.clear()
            self.rehighlight()
            return
        changes = []
        for source, other in ((old, new), (new, old)):
            if source is None:
                continue
            lo, hi = source
            if other is None:
                changes.append((lo, hi))
            else:
                if lo < other[0]: changes.append((lo, min(hi, other[0] - 1)))
                if hi > other[1]: changes.append((max(lo, other[1] + 1), hi))
        for lo, hi in changes:
            block = doc.findBlockByNumber(lo)
            while block.isValid() and block.blockNumber() <= hi:
                self.rehighlightBlock(block)
                block = block.next()
        # Table headers depend on the following separator row. Qt's normal
        # forward state propagation does not revisit that preceding row.
        neighbors, self._dirty_neighbors = self._dirty_neighbors, []
        for block in neighbors:
            if block.isValid(): self.rehighlightBlock(block)
        self.editor.viewport().update()

    def highlightBlock(self, text: str):
        self._highlight_block(text)
        if self.previousBlockState() == 1 or self.currentBlockState() == 1:
            return
        marker = BLOCK_ID.search(text)
        if marker:
            fmt = QTextCharFormat()
            if self.currentBlock().blockNumber() == self.active_block_num or self.has_selection:
                fmt.setForeground(theme_manager.color('text.secondary'))
            else:
                fmt.setForeground(QColor(Qt.transparent))
                fmt.setFontPointSize(0.01)
            start = len(text[:marker.start()].encode('utf-16-le')) // 2
            length = len(text[marker.start():].encode('utf-16-le')) // 2
            self.setFormat(start,length,fmt)

    def _highlight_block(self, text: str):
        if not text:
            self.setCurrentBlockState(1 if self.previousBlockState() == 1 else 0)
            return

        current_block = self.currentBlock()
        b_num = current_block.blockNumber()

        if self.has_selection:
            b_start = current_block.position()
            b_end = b_start + current_block.length()
            is_active_line = (b_start < self.sel_end and b_end > self.sel_start)
        else:
            is_active_line = (b_num == self.active_block_num and self.active_block_num >= 0)

        # 1. Многострочные блоки кода ```
        in_code_block = (self.previousBlockState() == 1)
        stripped = text.strip()

        if stripped.startswith("```"):
            if in_code_block:
                self.setCurrentBlockState(0)
            else:
                self.setCurrentBlockState(1)

            if is_active_line:
                fence_active_fmt = QTextCharFormat()
                fence_active_fmt.setFontFamily("Fira Code")
                fence_active_fmt.setFontPointSize(9)
                fence_active_fmt.setForeground(theme_manager.color('text.primary'))  # Тег ``` белый
                fence_active_fmt.setFontWeight(QFont.Bold)
                self.setFormat(0, len(text), fence_active_fmt)
            else:
                lang = stripped[3:].strip()
                if lang:
                    tag_zero = QTextCharFormat()
                    tag_zero.setFontPointSize(0.01)
                    tag_zero.setForeground(QColor(0, 0, 0, 0))
                    lang_start = text.find(lang)
                    self.setFormat(0, lang_start, tag_zero)

                    lang_fmt = QTextCharFormat()
                    lang_fmt.setFontFamily("monospace")
                    # Preview uses a small, muted code-block label (~10 CSS px).
                    lang_fmt.setFontPointSize(7.5)
                    lang_fmt.setFontWeight(QFont.Normal)
                    lang_fmt.setForeground(theme_manager.color('text.secondary'))
                    self.setFormat(lang_start, len(lang), lang_fmt)
                else:
                    fence_pad_fmt = QTextCharFormat()
                    fence_pad_fmt.setFontFamily("Fira Code")
                    fence_pad_fmt.setForeground(QColor(0, 0, 0, 0))
                    self.setFormat(0, len(text), fence_pad_fmt)
            return

        if in_code_block:
            self.setCurrentBlockState(1)
            code_body_fmt = QTextCharFormat()
            code_body_fmt.setFontFamily("monospace")
            code_body_fmt.setForeground(theme_manager.color('text.primary'))
            self.setFormat(0, len(text), code_body_fmt)
            return

        self.setCurrentBlockState(0)

        tag_hidden = QTextCharFormat()
        tag_hidden.setFontPointSize(0.01)
        tag_hidden.setForeground(QColor(0, 0, 0, 0))

        # СИМВОЛЫ РАЗМЕТКИ — БЕЛЫЕ
        tag_active = QTextCharFormat()
        tag_active.setForeground(theme_manager.color('text.primary'))
        tag_active.setFontWeight(QFont.Bold)

        # H6+ is intentionally unsupported in Toshy Markdown. If a line starts
        # with six or more hashes, leave the entire line as literal/plain text.
        # Removing the sixth hash immediately makes the line eligible for H5.
        if re.match(r"^\s*#{6,}", text):
            return

        # 2. Горизонтальный разделитель (---, ***, ___)
        if re.match(r"^\s*(---|___|\*\*\*)\s*$", text):
            if is_active_line:
                self.setFormat(0, len(text), tag_active)
            else:
                self.setFormat(0, len(text), tag_hidden)
            return

        # 3. Заголовки (# .. #####). Six or more leading hashes stay plain text.
        header_match = re.match(r"^(#{1,5})(?!#)\s+(.*)$", text)
        if header_match:
            hashes = header_match.group(1)
            level = len(hashes)
            content_start = header_match.start(2)
            content_len = len(header_match.group(2))
            tag_len = len(hashes) + 1

            h_format = QTextCharFormat()
            h_format.setFontPointSize(HEADING_POINTS[level])
            h_format.setFontWeight(QFont.Bold)
            h_format.setForeground(theme_manager.color('text.secondary'))

            self.setFormat(0, tag_len, tag_active if is_active_line else tag_hidden)
            self.setFormat(content_start, content_len, h_format)
            return

        # 4. Цитаты (>)
        quote_match = re.match(r"^(\s*\>)(\s*)(.*)$", text)
        if quote_match:
            q_tag_len = len(quote_match.group(1))
            q_space_start = q_tag_len
            q_space_len = len(quote_match.group(2))
            q_text_start = q_space_start + q_space_len
            q_text_len = len(quote_match.group(3))

            q_format = QTextCharFormat()
            q_format.setFontItalic(True)
            q_format.setForeground(theme_manager.color('text.secondary'))

            transparent_fmt = QTextCharFormat()
            transparent_fmt.setForeground(QColor(0, 0, 0, 0))

            self.setFormat(0, q_tag_len, tag_active if is_active_line else transparent_fmt)

            if not is_active_line and q_space_len > 0:
                space_fmt = QTextCharFormat(q_format)
                font = space_fmt.font()
                font.setLetterSpacing(QFont.AbsoluteSpacing, 16.0)
                space_fmt.setFont(font)
                self.setFormat(q_space_start, q_space_len, space_fmt)
            else:
                self.setFormat(q_space_start, q_space_len, q_format)

            self.setFormat(q_text_start, q_text_len, q_format)

        # 5. Списки (Маркированные: -, +, *) — динамическая компенсация ширины символа
        list_match = re.match(r"^(\s*)([-*+])(\s+)(.*)$", text)
        if list_match and not re.match(r"^\s*(\*\*|__)", text):
            marker_start = len(list_match.group(1))
            marker_char = list_match.group(2)

            bullet_format = QTextCharFormat()
            bullet_format.setForeground(theme_manager.color('text.primary'))
            bullet_format.setFontWeight(QFont.Bold)

            fm = self.editor.fontMetrics()
            char_w = fm.horizontalAdvance(marker_char)
            target_indent = 16.0
            letter_spacing = max(0.0, target_indent - char_w)

            transparent_fmt = QTextCharFormat()
            transparent_fmt.setForeground(QColor(0, 0, 0, 0))
            font = transparent_fmt.font()
            font.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing)
            transparent_fmt.setFont(font)

            self.setFormat(marker_start, 1, bullet_format if is_active_line else transparent_fmt)

        # 6. Списки (Нумерованные) — сноска и сдвиг вправо
        num_list_match = re.match(r"^(\s*)(\d+[\.\)])(\s+)(.*)$", text)
        if num_list_match:
            num_start = len(num_list_match.group(1))
            num_str = num_list_match.group(2)
            num_len = len(num_str)

            active_num_format = QTextCharFormat()
            active_num_format.setForeground(theme_manager.color('text.primary'))
            active_num_format.setFontWeight(QFont.Bold)

            if is_active_line:
                self.setFormat(num_start, num_len, active_num_format)
            else:
                fm = self.editor.fontMetrics()
                num_w = fm.horizontalAdvance(num_str)
                target_num_indent = max(20.0, num_w + 10.0)
                letter_spacing = max(0.0, target_num_indent - num_w)

                transparent_num_fmt = QTextCharFormat()
                transparent_num_fmt.setForeground(QColor(0, 0, 0, 0))
                font = transparent_num_fmt.font()
                font.setLetterSpacing(QFont.AbsoluteSpacing, letter_spacing / max(1, num_len))
                transparent_num_fmt.setFont(font)

                self.setFormat(num_start, num_len, transparent_num_fmt)

        # 7. Жирный курсив (***текст*** или ___текст___)
        for m in re.finditer(r"(?<!\*)\*\*\*(?!\*)(.*?)(?<!\*)\*\*\*(?!\*)|(?<!_)___(?!_)(.*?)(?<!_)___(?!_)", text):
            content = m.group(1) if m.group(0).startswith("***") else m.group(2)
            c_len = len(content)

            bi_format = QTextCharFormat()
            bi_format.setFontWeight(QFont.Bold)
            bi_format.setFontItalic(True)
            bi_format.setForeground(theme_manager.color('text.primary'))  # Текст серый

            t1_start = m.start()
            c_start = t1_start + 3
            t2_start = c_start + c_len

            self.setFormat(t1_start, 3, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 3, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, bi_format)

        # 8. Жирный шрифт (**текст** или __текст__)
        for m in re.finditer(r"(?<!\*)\*\*(?!\*)(.*?)(?<!\*)\*\*(?!\*)|(?<!_)__(?!_)(.*?)(?<!_)__(?!_)", text):
            content = m.group(1) if m.group(0).startswith("**") else m.group(2)
            c_len = len(content)

            b_format = QTextCharFormat()
            b_format.setFontWeight(QFont.Bold)
            b_format.setForeground(theme_manager.color('text.secondary'))  # Текст серый

            t1_start = m.start()
            c_start = t1_start + 2
            t2_start = c_start + c_len

            self.setFormat(t1_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, b_format)

        # 9. Выделение маркером (==текст==)
        for m in re.finditer(r"(?<!=)==(?!=)(.*?)(?<!=)==(?!=)", text):
            content = m.group(1)
            c_len = len(content)

            hl_format = QTextCharFormat()
            hl_format.setBackground(theme_manager.color('control.selected'))
            hl_format.setForeground(theme_manager.color('text.primary'))  # Текст серый
            hl_format.setFontWeight(QFont.DemiBold)

            t1_start = m.start()
            c_start = t1_start + 2
            t2_start = c_start + c_len

            self.setFormat(t1_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, hl_format)

        # 10. Зачеркнутый текст (~~текст~~)
        for m in re.finditer(r"(?<!~)~~(?!~)(.*?)(?<!~)~~(?!~)", text):
            content = m.group(1)
            c_len = len(content)

            s_format = QTextCharFormat()
            s_format.setFontStrikeOut(True)
            s_format.setForeground(theme_manager.color('text.secondary'))  # Текст приглушенный серый

            t1_start = m.start()
            c_start = t1_start + 2
            t2_start = c_start + c_len

            self.setFormat(t1_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 2, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, s_format)

        # 11. Курсив (*текст* или _текст_)
        for m in re.finditer(r"(?<!\*)\*(?!\*)(.*?)(?<!\*)\*(?!\*)", text):
            t1_start, t2_start = m.start(), m.end() - 1
            c_start, c_len = m.start(1), len(m.group(1))

            i_format = QTextCharFormat()
            i_format.setFontItalic(True)
            i_format.setForeground(theme_manager.color('text.secondary'))  # Текст серый

            self.setFormat(t1_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, i_format)

        for m in re.finditer(r"(?<!\w)_(?!_)(.*?)(?<!_)_(?!\w)", text):
            t1_start, t2_start = m.start(), m.end() - 1
            c_start, c_len = m.start(1), len(m.group(1))

            i_format = QTextCharFormat()
            i_format.setFontItalic(True)
            i_format.setForeground(theme_manager.color('text.secondary'))  # Текст серый

            self.setFormat(t1_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, i_format)

        # 12. Инлайн-код (`код`)
        for m in re.finditer(r"(`)(.*?)\1", text):
            t1_start, t2_start = m.start(1), m.end(2)
            c_start, c_len = m.start(2), len(m.group(2))

            code_format = QTextCharFormat()
            code_format.setForeground(theme_manager.color('text.primary'))  # Текст инлайн-кода серый
            code_format.setBackground(theme_manager.color('background.panel'))
            code_format.setFontFamily("Fira Code")

            self.setFormat(t1_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(t2_start, 1, tag_active if is_active_line else tag_hidden)
            self.setFormat(c_start, c_len, code_format)

        # 13. Ссылки [Текст](https://...)
        for m in re.finditer(r"(\[)(.*?)(\]\()([^\)]+)(\))", text):
            open_sq = m.start(1)
            txt_start, txt_len = m.start(2), len(m.group(2))
            mid_part = m.start(3)
            mid_len = len(m.group(3))
            url_start, url_len = m.start(4), len(m.group(4))
            close_par = m.start(5)

            link_format = QTextCharFormat()
            link_format.setForeground(theme_manager.color('text.secondary'))  # Текст ссылки серый
            link_format.setFontUnderline(True)

            url_format = QTextCharFormat()
            url_format.setForeground(theme_manager.color('text.secondary'))  # URL серый
            url_format.setFontUnderline(True)

            if is_active_line:
                self.setFormat(open_sq, 1, tag_active)  # Скобки белые
                self.setFormat(txt_start, txt_len, link_format)
                self.setFormat(mid_part, mid_len, tag_active)
                self.setFormat(url_start, url_len, url_format)
                self.setFormat(close_par, 1, tag_active)
            else:
                self.setFormat(open_sq, 1, tag_hidden)
                self.setFormat(txt_start, txt_len, link_format)
                self.setFormat(mid_part, mid_len, tag_hidden)
                self.setFormat(url_start, url_len, tag_hidden)
                self.setFormat(close_par, 1, tag_hidden)

        # 14. Прямые ссылки
        for m in re.finditer(r"(?<!\()(https?://[^\s\)]+)", text):
            raw_url_fmt = QTextCharFormat()
            raw_url_fmt.setForeground(theme_manager.color('accent.primary'))
            raw_url_fmt.setFontUnderline(True)
            self.setFormat(m.start(), len(m.group(0)), raw_url_fmt)

        # 15. Таблицы (строки с разделителями '|')
        if "|" in text and not in_code_block:
            # Строка-разделитель таблицы (| :--- | :---: |) — отображаем обычным приглушенным текстом
            if re.match(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$", text):
                sep_fmt = QTextCharFormat()
                sep_fmt.setFontFamily("Fira Code")
                sep_fmt.setForeground(theme_manager.color('border.subtle'))
                self.setFormat(0, len(text), sep_fmt)
                return
            else:
                # Пайпы таблицы: приглушенный темно-серый разделитель колонок
                pipe_fmt = QTextCharFormat()
                pipe_fmt.setFontFamily("Fira Code")
                pipe_fmt.setForeground(theme_manager.color('border.subtle'))
                pipe_fmt.setFontWeight(QFont.Bold)
                for m in re.finditer(r"\|", text):
                    self.setFormat(m.start(), 1, pipe_fmt)

                # Шапка таблицы (если следующая строка — разделитель): выделяем жирным белым цветом
                next_block = current_block.next()
                if next_block.isValid() and re.match(r"^\s*\|?(\s*:?-{2,}:?\s*\|)+\s*:?-{2,}:?\s*\|?\s*$", next_block.text()):
                    header_fmt = QTextCharFormat()
                    header_fmt.setFontWeight(QFont.Bold)
                    header_fmt.setForeground(theme_manager.color('text.primary'))
                    for cell_match in re.finditer(r"\|([^|\n]+)", text):
                        self.setFormat(cell_match.start(1), len(cell_match.group(1)), header_fmt)
