"""Explicit destinations for files; ordinary SVG activation stays in File."""
from ui.i18n import tr

import re
from pathlib import Path

from PySide6.QtCore import QMimeData, QUrl
from PySide6.QtGui import QImageReader
from ui.widgets.dialogs import MessageBox as QMessageBox


IGNORED_BINARY_EXTS = {
    '.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.bmp', '.tif', '.tiff', '.avif',
    '.zip', '.tar', '.gz', '.7z', '.rar', '.exe', '.bin', '.tpaint',
    '.pdf', '.mp4', '.mp3', '.wav', '.woff', '.woff2', '.ttf', '.otf',
}
OPEN_TARGETS = (('file', 'File'), ('file_text','File (Text)'), ('file_image','File (Image)'),
                ('board','Board'), ('board_image', 'Board (Image)'),
                ('board_text', 'Board (Text)'), ('sketch', 'Sketch'))


def is_text_file(path):
    path = Path(path)
    if not path.is_file() or path.suffix.lower() in IGNORED_BINARY_EXTS:
        return False
    try:
        with path.open('rb') as source:
            return b'\x00' not in source.read(4096)
    except OSError:
        return False


def file_targets(path):
    path = Path(path)
    if not path.is_file():
        return dict.fromkeys((key for key, _ in OPEN_TARGETS), False)
    text = is_text_file(path)
    image = QImageReader(str(path)).canRead()
    return {'file': text or image, 'file_text':text, 'file_image':image,
            'board':text or image or path.suffix.lower()=='.canvas',
            'board_text': text, 'board_image': image,
            'sketch': image or path.suffix.lower() == '.tpaint'}


def _board_text(path):
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    if path.suffix.lower() in {'.md', '.markdown'}:
        return text
    # Preserve source literally, including embedded ``` and SVG markup. SVG
    # remains a normal code card with its existing Render and Fold controls.
    longest = max((len(m[0]) for m in re.finditer(r'`+', text)), default=0)
    fence = '`' * max(3, longest + 1)
    language = {'.py': 'python', '.js': 'javascript', '.ts': 'typescript',
                '.html': 'html', '.xml': 'xml', '.svg': 'svg'}.get(path.suffix.lower(), '')
    return fence + language + '\n' + text + '\n' + fence


def open_file_in(window, file_path, target):
    path = Path(file_path).resolve()
    if target not in dict(OPEN_TARGETS):
        return False
    view = (getattr(window, 'file_view', None) if target.startswith('file') else
            getattr(window, 'paint_view', None) if target == 'sketch' else
            getattr(window, 'board_view', None))
    label = dict(OPEN_TARGETS)[target]
    if view is None:
        QMessageBox.information(window, tr('ui.file_opening.open_in_f43d'),
            label + tr('ui.caption.133a2649c409'))
        return False
    if not file_targets(path).get(target):
        QMessageBox.warning(window, tr('ui.file_opening.open_in_f43d'), tr('ui.caption.1526d8844954') + label + '.')
        return False
    try:
        if target.startswith('file'):
            as_image = target=='file_image' or (target=='file' and not is_text_file(path))
            if as_image:
                view.open_image_file(str(path))
            else:
                view.open_file(str(path), force=True)
            index = 2
        elif target == 'sketch':
            # SVG is rasterized into a new Sketch draft, never overwrites the
            # source SVG and is not intercepted by ordinary double-clicks.
            view.open_file(str(path), as_image=True)
            index = 4
        else:
            if target=='board' and path.suffix.lower()=='.canvas':
                view.open_board(str(path))
                from ui.event_bus import event_bus
                event_bus.switch_view_tab_requested.emit(3)
                return True
            if target=='board':
                target = 'board_image' if QImageReader(str(path)).canRead() else 'board_text'
            text = _board_text(path) if target == 'board_text' else None
            if view.tab_bar.count() == 0:
                view._create_new_untitled_board()
            view._on_current_tab_changed(view.tab_bar.currentIndex())
            document = view.stack.currentWidget()
            canvas = getattr(document, 'canvas', None)
            if canvas is None or canvas.read_only:
                raise ValueError('Текущая доска недоступна для редактирования.')
            if target == 'board_image':
                from ui.views.inline_images import import_images
                mime = QMimeData()
                mime.setUrls([QUrl.fromLocalFile(str(path))])
                imported = import_images(mime, document.file_path)
                if not imported:
                    raise ValueError('Не удалось загрузить изображение.')
                text = '\n'.join(imported)
            center = canvas.mapToScene(canvas.viewport().rect().center())
            card = canvas.create_card(center.x(), center.y(), text=text)
            card._on_editing_finished()
            canvas.zoom_to_card(card)
            index = 3
        from ui.event_bus import event_bus
        event_bus.switch_view_tab_requested.emit(index)
        return True
    except (OSError, ValueError) as error:
        QMessageBox.warning(window, tr('ui.file_opening.open_in_f43d'), str(error))
        return False
