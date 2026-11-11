"""Per-source-line formatting; file bytes remain ordinary Markdown/plain text."""
import hashlib
import json
import os
import math
from pathlib import Path
from PySide6.QtCore import Qt,QRectF
from ui.settings import QSettings
from PySide6.QtGui import QTextCursor,QTextBlockFormat,QTextOption,QTextFormat

ALIGNMENTS={'left':Qt.AlignLeft,'center':Qt.AlignHCenter,'right':Qt.AlignRight}


def name(block):
    value=block.blockFormat().alignment()
    return 'center' if value & Qt.AlignHCenter else 'right' if value & Qt.AlignRight else 'left'


def mapping(editor):
    if not getattr(editor,'is_markdown',False):return {}
    result={};block=editor.document().firstBlock()
    while block.isValid():
        value=name(block)
        if value!='left':result[block.blockNumber()]=value
        block=block.next()
    return result


def apply_visible(editor):
    block=editor.firstVisibleBlock()
    while block.isValid():
        editor.blockBoundingRect(block)
        geometry=editor.blockBoundingGeometry(block).translated(editor.contentOffset())
        if geometry.top()>editor.viewport().height():break
        layout=block.layout();option=QTextOption(layout.textOption())
        value=ALIGNMENTS[name(block)] if getattr(editor,'is_markdown',False) else Qt.AlignLeft
        if option.alignment()!=value:
            option.setAlignment(value);layout.setTextOption(option)
        block=block.next()


def set_selected(editor,value):
    if editor.isReadOnly() or not getattr(editor,'is_markdown',False):return
    cursor=editor.textCursor();start,end=cursor.selectionStart(),cursor.selectionEnd()
    first=editor.document().findBlock(start)
    last=editor.document().findBlock(max(start,end-1)).blockNumber()
    cursor.beginEditBlock()
    while first.isValid() and first.blockNumber()<=last:
        change=QTextCursor(first);fmt=QTextBlockFormat();fmt.setAlignment(ALIGNMENTS[value]);change.mergeBlockFormat(fmt)
        first=first.next()
    cursor.endEditBlock();editor.viewport().update()


def _key(path):
    return hashlib.sha256(os.path.normcase(os.path.abspath(path)).encode()).hexdigest()


def save(path,editor):
    if Path(path).suffix.lower() not in {'.md','.markdown'}:return
    blocks=[];block=editor.document().firstBlock()
    while block.isValid():
        value=name(block)
        if value!='left':blocks.append([block.blockNumber(),hashlib.sha256(block.text().encode()).hexdigest(),value])
        block=block.next()
    settings=QSettings('Toshy','TextFormats')
    if blocks:settings.setValue(_key(path),json.dumps(blocks))
    else:settings.remove(_key(path))
    settings.sync()


def restore(path,editor):
    if Path(path).suffix.lower() not in {'.md','.markdown'}:return
    settings=QSettings('Toshy','TextFormats')
    try:data=json.loads(settings.value(_key(path),'[]'))
    except (TypeError,ValueError):return
    if not isinstance(data, list):
        return
    for item in data:
        if not isinstance(item, list) or len(item) != 3:
            continue
        number, digest, value = item
        if type(number) is not int or not isinstance(value, str) or value not in ALIGNMENTS:
            continue
        block = editor.document().findBlockByNumber(number)
        if block.isValid() and hashlib.sha256(block.text().encode()).hexdigest() == digest:
            cursor = QTextCursor(block)
            fmt = QTextBlockFormat()
            fmt.setAlignment(ALIGNMENTS[value])
            cursor.mergeBlockFormat(fmt)


def paint_selection_gutters(editor, painter):
    """Qt starts FullWidthSelection at the aligned text's left edge.

    Extend just the empty gutter; glyphs, selections, caret and heading flashes
    continue to be painted by QPlainTextEdit. Work is bounded to selections on
    screen and uses existing QTextLine geometry, including wrapped lines.
    """
    painter.save()
    painter.setRenderHint(painter.RenderHint.Antialiasing, False)
    for selection in editor.extraSelections():
        if not selection.format.property(QTextFormat.FullWidthSelection):
            continue
        cursor = selection.cursor
        block = cursor.block()
        if not block.isValid() or not block.isVisible():
            continue
        line = block.layout().lineForTextPosition(cursor.positionInBlock())
        if not line.isValid():
            continue
        origin = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).topLeft()
        top = math.floor(origin.y() + line.y())
        bottom = math.ceil(origin.y() + line.y() + line.height())
        left = math.floor(origin.x() + line.naturalTextRect().left())
        if left > 0 and bottom > 0 and top < editor.viewport().height():
            painter.fillRect(QRectF(0, top, min(left, editor.viewport().width()), bottom-top),
                             selection.format.background())
    painter.restore()
