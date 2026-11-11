"""Atomic, plain-text-on-send quote objects inside the ordinary Qt composer."""
import json
import uuid

from PySide6.QtCore import Qt, QRectF, QSizeF, QMimeData, QObject
from PySide6.QtGui import (QTextObjectInterface, QTextFormat, QTextCharFormat,
                          QTextCursor, QTextBlockFormat, QColor, QFontMetricsF)
try:
    from PySide6.QtGui import QPyTextObject as _QuoteObjectBase
except ImportError:
    class _QuoteObjectBase(QObject, QTextObjectInterface):
        def __init__(self, parent=None):
            QObject.__init__(self, parent)

QUOTE_TYPE = QTextFormat.UserObject.value + 41
QUOTE_DATA = QTextFormat.UserProperty.value + 41
QUOTE_SEPARATOR = QTextFormat.UserProperty.value + 42


def utf16_slice(text, start, end):
    return text.encode("utf-16-le")[start * 2:end * 2].decode("utf-16-le")


class QuoteObjectRenderer(_QuoteObjectBase):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def intrinsicSize(self, document, position, fmt):
        data = json.loads(fmt.property(QUOTE_DATA))
        width = max(40.0, self.editor.viewport().width() - 2 * document.documentMargin() - 2)
        metrics = QFontMetricsF(self.editor.font())
        text_rect = metrics.boundingRect(QRectF(0, 0, max(10, width - 36), 100000),
                                        Qt.TextWordWrap, data["text"] or " ")
        line = metrics.lineSpacing()
        height = max(line, text_rect.height()) + 4
        return QSizeF(width, height)

    def drawObject(self, painter, rect, document, position, fmt):
        data = json.loads(fmt.property(QUOTE_DATA))
        editor = self.editor
        # Qt supplies document coordinates and a painter transform to the viewport.
        viewport_rect = painter.worldTransform().mapRect(rect)
        editor._quote_rects[data["uid"]] = viewport_rect
        painter.save()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#24242c"))
        painter.drawRoundedRect(rect.adjusted(0, 1, -1, -1), 3, 3)
        painter.setPen(QColor("#a5b4fc"))
        painter.drawLine(rect.topLeft(), rect.bottomLeft())
        painter.setFont(editor.font())
        painter.setPen(QColor("#c9c9d2"))
        painter.drawText(rect.adjusted(8, 2, -28, -2), Qt.TextWordWrap, data["text"])
        if editor._hover_quote == data["uid"]:
            painter.setPen(QColor("#a1a1aa"))
            painter.drawText(QRectF(rect.right() - 24, rect.top(), 22, rect.height()),
                             Qt.AlignCenter, "×")
        painter.restore()


class InlineQuoteMixin:
    def init_inline_quotes(self):
        self._quote_rects = {}
        self._hover_quote = None
        self._quote_renderer = QuoteObjectRenderer(self)
        self.document().documentLayout().registerHandler(QUOTE_TYPE, self._quote_renderer)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.document().contentsChange.connect(self._quote_contents_changed)

    def _quote_contents_changed(self, _position, removed, added):
        if removed or added:
            self._quote_rects.clear()
            self.quotes_changed.emit()

    def quote_objects(self):
        result = []
        block = self.document().begin()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                fmt = fragment.charFormat()
                if fmt.objectType() == QUOTE_TYPE:
                    data = json.loads(fmt.property(QUOTE_DATA))
                    # An object is always one UTF-16 character.
                    offset = 0
                    for character in fragment.text():
                        if character == "\ufffc":
                            result.append(dict(data, position=fragment.position() + offset))
                        offset += len(character.encode("utf-16-le")) // 2
                it += 1
            block = block.next()
        return result

    def toPlainText(self):
        encoded = self.document().toPlainText().encode("utf-16-le")
        for quote in reversed(self.quote_objects()):
            position = quote["position"] * 2
            encoded = encoded[:position] + ('"' + quote["text"] + '"').encode("utf-16-le") + encoded[position + 2:]
        text = encoded.decode("utf-16-le")
        # Убираем только наш пустой хвост. Пользовательские Enter сохраняются.
        block = self.document().lastBlock()
        marker = block.blockFormat().property(QUOTE_SEPARATOR) or {}
        if not block.text() and marker and text.endswith('\n'):
            count = 0
            scan = self.document().begin()
            while scan.isValid():
                value = scan.blockFormat().property(QUOTE_SEPARATOR) or {}
                count += value.get('id') == marker.get('id')
                scan = scan.next()
            if count == 1:
                text = text[:-1]
        return text

    def add_quote(self, text, *, target, source=None, start=0, end=0):
        data = {"uid": uuid.uuid4().hex, "text": text, "target": target,
                "source": source, "start": start, "end": end,
                "prefix_breaks": [], "suffix_breaks": []}
        cursor = self.textCursor()
        # Keep any selected comment rather than replacing it while quoting chat.
        cursor.clearSelection()
        data["suffix_tail"] = utf16_slice(cursor.block().text(), cursor.positionInBlock(),
                                          len(cursor.block().text().encode("utf-16-le")) // 2)
        cursor.beginEditBlock()
        if cursor.position() > 0:
            raw = self.document().toPlainText().encode("utf-16-le")[:cursor.position() * 2].decode("utf-16-le")
            trailing = len(raw) - len(raw.rstrip("\n"))
            previous_position = cursor.position() - trailing - 1
            adjacent_quote = any(item['position'] == previous_position for item in self.quote_objects())
            # Leave an empty editable line between consecutive quote cards.
            needed = max(0, (2 if adjacent_quote else 1) - trailing)
            if needed:
                data["prefix_breaks"] = self._insert_quote_breaks(cursor, needed, data["uid"])
            else:
                # A preceding quote may have provided this gap. Retain it while
                # that quote exists, but reclaim it if that quote is removed first.
                data["shared_prefix"] = self._separator_ids(cursor.position() - 1, 1)
        data["suffix_breaks"] = [uuid.uuid4().hex]
        fmt = QTextCharFormat()
        fmt.setObjectType(QUOTE_TYPE)
        fmt.setProperty(QUOTE_DATA, json.dumps(data, ensure_ascii=False))
        cursor.insertText("\ufffc", fmt)
        self._insert_quote_breaks(cursor, 1, data["uid"], data["suffix_breaks"])
        cursor.endEditBlock()
        self.setTextCursor(cursor)
        self.setCurrentCharFormat(QTextCharFormat())
        self.ensureCursorVisible()
        return data["uid"]

    def _insert_quote_breaks(self, cursor, count, owner, ids=None):
        ids = ids or [uuid.uuid4().hex for _ in range(count)]
        for identity in ids:
            block_format = QTextBlockFormat(cursor.blockFormat())
            block_format.setProperty(QUOTE_SEPARATOR, {"id": identity, "owner": owner})
            cursor.insertBlock(block_format, QTextCharFormat())
        return ids

    def _separator_ids(self, position, count):
        ids = []
        for offset in range(count):
            block = self.document().findBlock(position + offset + 1)
            marker = block.blockFormat().property(QUOTE_SEPARATOR) or {}
            if block.position() != position + offset + 1 or not marker:
                return []
            ids.append(marker["id"])
        return ids

    def _removable_quote_gap(self, quote, side, ids, *, shared=False):
        if not ids:
            return None
        position = quote["position"]
        start = position - len(ids) if side == "prefix" else position + 1
        end = start + len(ids)
        if start < 0 or self._separator_ids(start, len(ids)) != ids:
            return None
        live_owners = {item["uid"] for item in self.quote_objects()}
        occurrences = {identity: 0 for identity in ids}
        block = self.document().begin()
        while block.isValid():
            marker = block.blockFormat().property(QUOTE_SEPARATOR) or {}
            identity = marker.get("id")
            if identity in occurrences:
                occurrences[identity] += 1
                # Enter inherits the block format: duplicated markers mean the
                # user has changed the gap, so it is no longer ours to remove.
                if shared and marker.get("owner") in live_owners:
                    return None
            block = block.next()
        if any(count != 1 for count in occurrences.values()):
            return None
        for offset in range(len(ids)):
            block = self.document().findBlock(start + offset + 1)
            text = block.text()
            if side == "suffix" and offset == len(ids) - 1:
                if text != quote.get("suffix_tail", ""):
                    return None
                continue
            if side == "prefix" and offset == len(ids) - 1:
                # This block contains the quote being removed. Any other text
                # here is a comment and must retain its paragraph boundary.
                text = text.replace("\ufffc", "", 1)
            if text:
                return None
        return start, end

    def remove_quote(self, uid):
        for quote in self.quote_objects():
            if quote["uid"] == uid:
                cursor = QTextCursor(self.document())
                ranges = [(quote["position"], quote["position"] + 1)]
                for side in ("prefix", "suffix"):
                    gap = self._removable_quote_gap(quote, side, quote.get(side + "_breaks", []))
                    if gap:
                        ranges.append(gap)
                shared = self._removable_quote_gap(quote, "prefix", quote.get("shared_prefix", []), shared=True)
                if shared:
                    ranges.append(shared)
                cursor.beginEditBlock()
                for start, end in sorted(ranges, reverse=True):
                    cursor.setPosition(start)
                    cursor.setPosition(end, QTextCursor.KeepAnchor)
                    cursor.removeSelectedText()
                cursor.endEditBlock()
                return

    def remove_all_quotes(self):
        cursor = self.textCursor()
        cursor.beginEditBlock()
        for quote in reversed(self.quote_objects()):
            self.remove_quote(quote["uid"])
        cursor.endEditBlock()

    def quote_backspace(self, event):
        if event.key() != Qt.Key_Backspace or event.modifiers() != Qt.NoModifier:
            return False
        cursor = self.textCursor()
        if cursor.hasSelection():
            return False
        position = cursor.position()
        previous = position - 1
        if previous < 0 or self.document().characterAt(previous) != "\ufffc":
            return False
        fmt_cursor = QTextCursor(self.document())
        fmt_cursor.setPosition(previous)
        fmt_cursor.setPosition(previous + 1, QTextCursor.KeepAnchor)
        if fmt_cursor.charFormat().objectType() != QUOTE_TYPE:
            return False
        data = json.loads(fmt_cursor.charFormat().property(QUOTE_DATA))
        self.remove_quote(data["uid"])
        event.accept()
        return True

    def mouseMoveEvent(self, event):
        hover = next((uid for uid, rect in self._quote_rects.items()
                      if rect.contains(event.position())), None)
        if hover != self._hover_quote:
            self._hover_quote = hover
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_quote = None
        self.viewport().update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for uid, rect in self._quote_rects.items():
                if QRectF(rect.right() - 26, rect.top(), 26, rect.height()).contains(event.position()):
                    self.remove_quote(uid)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._quote_rects.clear()
        if self.quote_objects():
            self.document().markContentsDirty(0, self.document().characterCount())

    def createMimeDataFromSelection(self):
        cursor = self.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        raw = self.document().toPlainText().encode("utf-16-le")[start * 2:end * 2]
        for quote in reversed(self.quote_objects()):
            if start <= quote["position"] < end:
                position = (quote["position"] - start) * 2
                raw = raw[:position] + ('"' + quote["text"] + '"').encode("utf-16-le") + raw[position + 2:]
        mime = QMimeData()
        mime.setText(raw.decode("utf-16-le"))
        return mime
