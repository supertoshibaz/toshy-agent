"""Ссылки на заголовки и постоянные идентификаторы абзацев Markdown."""
import html
import re
import unicodedata
import uuid
from urllib.parse import unquote

HEADING = re.compile(r'^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$')
BLOCK_ID = re.compile(r'(?:^|\s)\^([A-Za-z0-9][A-Za-z0-9_-]*)\s*$')
LIST_ITEM = re.compile(r'^\s*(?:[-+*]|\d+[.)])\s+')
RULE = re.compile(r'^\s{0,3}(?:-{3,}|_{3,}|\*{3,})\s*$')


def strip_block_id(line):
    match = BLOCK_ID.search(line)
    return line[:match.start()].rstrip() if match else line


def heading_slug(text):
    text = strip_block_id(text)
    text = re.sub(r'!?\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'<[^>]*>', '', text)
    text = unicodedata.normalize('NFKC', html.unescape(text)).casefold()
    text = re.sub(r'[^\w\s-]', '', text)
    return re.sub(r'\s+', '-', text.strip())


def fenced_lines(lines):
    result, fence = set(), None
    for index, line in enumerate(lines):
        match = re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$', line)
        if fence:
            result.add(index)
            if match and match[1][0] == fence[0] and len(match[1]) >= len(fence) and not match[2].strip():
                fence = None
        elif match:
            fence = match[1]
            result.add(index)
    return result


def paragraph_start(lines, line, fenced):
    start = line
    if not strip_block_id(lines[start]).strip() and start and lines[start-1].strip():
        start -= 1
    if HEADING.match(lines[start]):
        return start
    while start > 0:
        previous = lines[start-1]
        if (not previous.strip() or start-1 in fenced or HEADING.match(previous)
                or RULE.match(previous) or '|' in previous
                or BLOCK_ID.search(previous) or LIST_ITEM.match(lines[start])):
            break
        start -= 1
    return start


def anchor_targets(source):
    lines = source.split('\n')
    fenced = fenced_lines(lines)
    targets, duplicates = {}, {}
    for index, line in enumerate(lines):
        if index in fenced:
            continue
        heading = HEADING.match(strip_block_id(line))
        if heading:
            slug = heading_slug(heading[2]) or 'section'
            candidate, suffix = slug, duplicates.get(slug, 0)
            while candidate in targets:
                suffix += 1
                candidate = f'{slug}-{suffix}'
            duplicates[slug] = suffix
            targets[candidate] = index
        marker = BLOCK_ID.search(line)
        if marker:
            targets.setdefault('^'+marker[1], paragraph_start(lines,index,fenced))
    return targets


def resolve_anchor(source, fragment):
    fragment = unquote(fragment).lstrip('#')
    targets = anchor_targets(source)
    if fragment in targets:
        return targets[fragment]
    if fragment.startswith('^'):
        return None
    return targets.get(heading_slug(fragment))


def link_target(source, line):
    """Возвращает якорь, подпись и место вставки метки, не изменяя файл."""
    lines = source.split('\n')
    fenced = fenced_lines(lines)
    if (not 0 <= line < len(lines) or line in fenced or not lines[line].strip()
            or lines[line].startswith(('    ', '\t')) or '|' in lines[line] or RULE.match(lines[line])):
        return None
    heading = HEADING.match(strip_block_id(lines[line]))
    if heading:
        anchor = next(key for key,row in anchor_targets(source).items() if row==line and not key.startswith('^'))
        return anchor, heading[2], None
    start = paragraph_start(lines,line,fenced)
    end = start
    while end+1 < len(lines):
        if BLOCK_ID.search(lines[end]):
            break
        following = lines[end+1]
        if (not following.strip() or end+1 in fenced or HEADING.match(following)
                or LIST_ITEM.match(following) or RULE.match(following) or '|' in following):
            break
        end += 1
    marker = BLOCK_ID.search(lines[end])
    identity = marker[1] if marker else 'block-'+uuid.uuid4().hex[:12]
    label = strip_block_id(lines[start]).strip()
    return '^'+identity, label, None if marker else end


def copy_link(editor, path, line, *, allow_marker_in_preview=False):
    from PySide6.QtGui import QTextCursor
    from PySide6.QtWidgets import QApplication
    target = link_target(editor.toPlainText(),line)
    if target is None:
        return False
    anchor, label, append_line = target
    if append_line is not None:
        if editor.isReadOnly() and not allow_marker_in_preview:
            return False
        cursor = QTextCursor(editor.document().findBlockByNumber(append_line))
        cursor.movePosition(QTextCursor.EndOfBlock)
        cursor.beginEditBlock()
        cursor.insertText(' '+anchor)
        cursor.endEditBlock()
    from urllib.parse import quote
    reference = '#' + quote(anchor, safe='^_-')
    label = label.replace('[','').replace(']','').strip() or 'Абзац'
    QApplication.clipboard().setText(f'[{label}]({reference})')
    return True
