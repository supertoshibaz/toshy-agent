"""The same ordered-list chain tracker for files and canvas text cards."""
import re
from PySide6.QtCore import QObject, QTimer
from PySide6.QtGui import QTextCursor


class OrderedLists(QObject):
    def __init__(self, handler):
        super().__init__(handler.editor)
        self.handler = handler
        self.editor = handler.editor
        self._renumbering_ordered_lists = False
        self._ordered_dirty_bounds = None
        self._ordered_chain_starts = []
        self._ordered_start_overrides = []
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.renumber)
        self.editor.document().contentsChange.connect(self.changed)
        self.timer.start(0)

    def reset(self):
        self._ordered_chain_starts = []
        self._ordered_start_overrides = []
        self._ordered_dirty_bounds = None

    def changed(self, position, removed, added):
        if getattr(self.editor, '_setting_plain_text', False):
            self.reset()
        images = getattr(self.handler, 'images', None)
        presentation = self.handler._syncing_table_objects or (images and images.syncing)
        for chain in self._ordered_chain_starts:
            old_start, old_end = chain['position'], chain['end']
            if (not self._renumbering_ordered_lists and not presentation
                    and removed and position <= old_start
                    and position+removed >= old_start+chain['length']
                    and position+removed < old_end):
                anchor = QTextCursor(self.editor.document())
                anchor.setPosition(min(position+added, self.editor.document().characterCount()-1))
                # Deleting just the first item's text leaves an empty paragraph.
                # The surviving chain still inherits its deliberate starting number.
                block = anchor.block()
                while block.isValid() and not block.text().strip() and block.next().isValid():
                    block = block.next()
                anchor.setPosition(block.position())
                self._ordered_start_overrides.append((anchor, chain['indent'], chain['seed']))
            def shifted(value):
                return value+added-removed if value >= position+removed else min(value, position)
            chain['position'], chain['end'] = shifted(old_start), shifted(old_end)
        if self._renumbering_ordered_lists or presentation:
            return
        start, end = position, min(position+added, self.editor.document().characterCount()-1)
        if self._ordered_dirty_bounds is not None:
            start = min(start, self._ordered_dirty_bounds[0].position())
            end = max(end, self._ordered_dirty_bounds[1].position())
        left, right = QTextCursor(self.editor.document()), QTextCursor(self.editor.document())
        left.setPosition(start); right.setPosition(end)
        self._ordered_dirty_bounds = (left, right)
        self.timer.start(0)

    @staticmethod
    def _indent_columns(prefix: str) -> int:
        columns = 0
        for ch in prefix:
            columns += 4 if ch == "\t" else 1
        return columns

    def renumber(self):
        if self._renumbering_ordered_lists or not self.handler.enabled:
            return
        doc = self.editor.document()
        bounds = self._ordered_dirty_bounds
        self._ordered_dirty_bounds = None
        first = doc.findBlock(bounds[0].position()) if bounds else doc.firstBlock()
        last = doc.findBlock(bounds[1].position()) if bounds else doc.lastBlock()
        # Blank paragraphs terminate every nesting level. Expand a local edit
        # to its whole affected chain, including chains joined by a deletion.
        while first.previous().isValid() and first.previous().text().strip():
            first = first.previous()
        while last.next().isValid() and last.next().text().strip():
            last = last.next()
        stack = []  # {indent, next}
        chain_starts = []
        overrides = self._ordered_start_overrides
        self._ordered_start_overrides = []
        replacements = []
        pattern = re.compile(r"^(\s*)(\d+)([.)])(\s+)(.*)$")
        block = first
        while block.isValid() and block.blockNumber() <= last.blockNumber():
            line = block.text()
            if self.handler.is_in_code_block(block.blockNumber()):
                stack.clear()
                block = block.next()
                continue
            m = pattern.match(line)
            if m:
                indent = self._indent_columns(m.group(1))
                while stack and stack[-1]["indent"] > indent:
                    stack.pop()
                state = stack[-1] if stack and stack[-1]["indent"] == indent else None
                current = int(m.group(2))
                if state is None:
                    expected = next((seed for anchor, level, seed in overrides
                                     if level == indent and anchor.block() == block), current)
                    anchor = QTextCursor(block)
                    chain = {'position': block.position(), 'end': block.position() + len(line),
                             'length': len(line), 'indent': indent, 'seed': expected, 'anchor': anchor}
                    chain_starts.append(chain)
                    state = {"indent": indent, "next": expected + 1, 'chain': chain}
                    stack.append(state)
                else:
                    expected = state["next"]
                    state["next"] = expected + 1
                    state['chain']['end'] = block.position() + len(line)
                if current != expected:
                    start = block.position() + len(m.group(1))
                    replacements.append((start, start + len(m.group(2)), str(expected)))
            elif line.strip():
                leading = re.match(r"^\s*", line).group(0)
                indent = self._indent_columns(leading)
                while stack and indent <= stack[-1]["indent"]:
                    stack.pop()
            else:
                # A blank line terminates the current ordered-list chain.
                # A later numbered list must start from its own first number.
                stack.clear()
            block = block.next()

        low, high = first.position(), last.position() + last.length()
        self._ordered_chain_starts = [chain for chain in self._ordered_chain_starts
                                     if not low <= chain['position'] < high] + chain_starts

        if not replacements or self.editor.isReadOnly():
            return

        cursor = self.editor.textCursor()
        old_anchor, old_pos = cursor.anchor(), cursor.position()

        def adjusted(pos):
            delta = 0
            for start, end, repl in replacements:
                if end <= pos:
                    delta += len(repl) - (end - start)
                elif start < pos < end:
                    return start + delta + len(repl)
            return pos + delta

        self._renumbering_ordered_lists = True
        edit = QTextCursor(self.editor.document())
        edit.beginEditBlock()
        try:
            for start, end, repl in reversed(replacements):
                edit.setPosition(start)
                edit.setPosition(end, QTextCursor.KeepAnchor)
                edit.insertText(repl)
        finally:
            edit.endEditBlock()
            self._renumbering_ordered_lists = False

        restored = QTextCursor(self.editor.document())
        restored.setPosition(max(0, min(adjusted(old_anchor), self.editor.document().characterCount() - 1)))
        restored.setPosition(max(0, min(adjusted(old_pos), self.editor.document().characterCount() - 1)), QTextCursor.KeepAnchor)
        self.editor.setTextCursor(restored)

