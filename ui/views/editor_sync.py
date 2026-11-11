"""Сравнение буфера с диском и проверенное авторство внешних изменений."""
from ui.i18n import add_caption_action, bind_caption, tr
import difflib
import hashlib
from pathlib import Path
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QMenu, QToolButton, QWidget, QWidgetAction, QHBoxLayout
from ui.icons import set_icon


def disk_version(path):
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None, None
    from core.text_files import decode_text
    try:text = decode_text(raw)[0]
    except UnicodeError:
        return 'undecodable:'+hashlib.sha256(raw).hexdigest(), None
    # Такой же хеш текста, как при загрузке, с универсальными переносами строк.
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    oid = hashlib.sha1(b'blob ' + str(len(raw)).encode('ascii') + b'\0' + raw).hexdigest()
    return digest, oid


def show_comparison(document):
    view = getattr(document.window(), 'file_view', None)
    if view is None:
        return
    try:
        disk = Path(document.file_path).read_text(encoding='utf-8', errors='replace')
    except FileNotFoundError:
        disk = ''
    diff = ''.join(difflib.unified_diff(disk.splitlines(True), document.editor.toPlainText().splitlines(True),
                   fromfile=tr('ui.caption.1b5e98496f5a'), tofile=tr('ui.caption.b437cb4850f4')))
    fence = '`' * max(3, max((len(s) for s in __import__('re').findall(r'`+', diff)), default=0) + 1)
    view.open_temporary_markdown(key='external_diff:' + document.file_path,
        title=tr('ui.caption.2ce0a838adee') + Path(document.file_path).name, content=f'{fence}diff\n{diff}\n{fence}',
        breadcrumb=document.file_path)


def conflict_choice(document):
    menu = QMenu(document)
    menu.setObjectName('FileManagerContextMenu')
    body = QWidget(menu)
    row = QHBoxLayout(body)
    row.setContentsMargins(6, 6, 6, 6)
    result = []
    for value, tip, icon in (
        ('cancel', tr('ui.caption.30da93807b2b'), 'cancel'),
        ('compare', tr('ui.caption.aebeaed17d4e'), 'arrows-left-right'),
        ('overwrite', tr('ui.caption.1a9ed394848f'), 'save'),
        ('reload', tr('ui.caption.2db648654842'), 'refresh'),
    ):
        button = QToolButton(body)
        button.setFixedSize(36, 36)
        set_icon(button, icon)
        bind_caption(button, tip, setter='setToolTip')
        bind_caption(button, tip, setter='setAccessibleName')
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(lambda _=False, choice=value: (result.append(choice), menu.close()))
        row.addWidget(button)
    action = QWidgetAction(menu)
    action.setDefaultWidget(body)
    add_caption_action(menu, 'addAction', action)
    menu.exec(document.path_label.mapToGlobal(QPoint(0, document.path_label.height())))
    menu.deleteLater()
    choice = result[0] if result else 'cancel'
    if choice == 'compare':
        show_comparison(document)
    return choice
