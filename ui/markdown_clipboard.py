"""Private Markdown clipboard: image references stay inside Toshy editors."""
import json
import os
import re
from pathlib import Path

from PySide6.QtCore import QMimeData

MIME = 'application/x-toshy-markdown-selection'


def payload(mime):
    if mime is None or not mime.hasFormat(MIME):return None
    raw = bytes(mime.data(MIME))
    if len(raw) > 16 * 1024 * 1024:return None
    try:data = json.loads(raw)
    except (ValueError, UnicodeError):return None
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('source'), str):return None
    images = data.get('images')
    if not isinstance(images, list) or len(images) > 10000:return None
    if any(not isinstance(x, dict) or not isinstance(x.get('line'), int)
           or not isinstance(x.get('path'), str) for x in images):return None
    return data


def image_paths(mime):
    data = payload(mime)
    if data is None:return []
    return list(dict.fromkeys(x['path'] for x in data['images'] if Path(x['path']).is_file()))


def combine_selections(mimes):
    source, images, external = '', [], []
    for mime in mimes:
        data = payload(mime)
        if data is None:continue
        if source:source += '\n\n---\n\n'
        offset = source.count('\n')
        images.extend({**image, 'line': image['line'] + offset} for image in data['images'])
        source += data['source']
        if mime.text().strip():external.append(mime.text().strip())
    result = QMimeData()
    result.setText('\n\n---\n\n'.join(external))
    result.setData(MIME, json.dumps({'version': 1, 'source': source, 'images': images}, ensure_ascii=False).encode('utf-8'))
    return result


def selection_mime(handler, cursor):
    from ui.views.inline_images import document_path
    source = handler.selected_source_text(cursor)
    block = cursor.document().findBlock(cursor.selectionStart()).blockNumber()
    return source_mime(source, document_path(handler.editor), starts_in_code=handler.is_in_code_block(block),
                       images_enabled=handler.enabled)


def source_mime(source, base=None, *, starts_in_code=False, images_enabled=True):
    from ui.views.inline_images import image_reference, resolve_image
    images, external = [], []
    first_line = source.splitlines()[0] if source else ''
    fence = ('```' if starts_in_code
             and not re.match(r'^\s{0,3}(`{3,}|~{3,})', first_line) else None)
    for number, line in enumerate(source.splitlines(keepends=True)):
        marker = re.match(r'^\s{0,3}(`{3,}|~{3,})', line)
        if marker:
            value = marker.group(1)
            if fence is None:fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):fence = None
            external.append(line)
            continue
        reference = image_reference(line) if images_enabled and fence is None else None
        if reference:
            path = resolve_image(reference[1], base)
            images.append({'line': number, 'path': str(path) if path else '', 'source': line.rstrip('\r\n')})
        else:
            external.append(line)
    mime = QMimeData()
    mime.setText(''.join(external))
    mime.setData(MIME, json.dumps({'version': 1, 'source': source, 'images': images}, ensure_ascii=False).encode('utf-8'))
    return mime


def editor_source(mime, base_path=None):
    """Rebase references without copying or duplicating the image files."""
    from ui.app_state import app_state
    from ui.views.inline_images import image_reference, with_image_size, image_size_mode
    data = payload(mime)
    if data is None:return None
    lines = data['source'].splitlines(keepends=True)
    base = Path(base_path).resolve().parent if base_path else Path(app_state.view_workspace('board')).resolve()
    for image in data['images']:
        index = image['line']
        if not 0 <= index < len(lines):continue
        reference = image_reference(lines[index])
        if reference is None or not image['path']:continue
        target = Path(image['path'])
        if not target.is_absolute():continue
        try:relative = Path(os.path.relpath(target, base)).as_posix()
        except ValueError:relative = target.as_uri()  # Different Windows drives.
        # Angle brackets/newlines have special meaning inside a Markdown URL.
        relative = relative.replace('<', '%3C').replace('>', '%3E').replace('\n', '%0A').replace('\r', '%0D')
        result = f'![{reference[0]}](<{relative}>)'
        mode = image_size_mode(lines[index])
        if mode != 'fit':result = with_image_size(result, mode)
        ending = '\r\n' if lines[index].endswith('\r\n') else '\n' if lines[index].endswith('\n') else ''
        lines[index] = result + ending
    return ''.join(lines)
