"""Рабочие копии вложений: относительные пути, атомарное копирование и свои права."""
import os
import re
import shutil
import uuid
from pathlib import Path
from core.attachments import AttachmentError, _safe_filename, resolve_attachment_path


def working_copy(attachment, workspace, session_id=None, source=None):
    if attachment.get('memory_only') or attachment.get('temp_memory'):
        raise AttachmentError('Это вложение передано во временную память; рабочая копия не создаётся.')
    root = Path(workspace).expanduser().resolve()
    identity = str(attachment.get('id') or '')
    if not re.fullmatch(r'att_[a-f0-9]{32}', identity):
        raise AttachmentError('Некорректный идентификатор вложения.')
    # Файл проекта остаётся живой ссылкой: не создаём вторую редактируемую версию.
    original = Path(source).expanduser().resolve() if source is not None else None
    if original is None and attachment.get('storage') == 'link':
        original = resolve_attachment_path(attachment, session_id)
    if (attachment.get('workspace_reference') is not False and original is not None
            and original.is_relative_to(root) and original.is_file()):
        attachment['workspace_reference'] = True
        return original.relative_to(root).as_posix(), original
    relative = Path('attachments') / identity / _safe_filename(attachment.get('filename', 'file'))
    target = (root / relative).resolve()
    if not target.is_relative_to(root):raise AttachmentError('Путь вложения выходит за пределы проекта.')
    if not target.is_file():
        original = original or resolve_attachment_path(attachment, session_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.part')
        try:
            shutil.copyfile(original, temporary)
            # copy2 перенёс бы ограничения доступа с внешнего оригинала.
            if os.name != 'nt':temporary.chmod(0o600)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return relative.as_posix(), target


def copy_to_project(source, target):
    """Явное действие пользователя: атомарно добавить файл в Project Folder."""
    from core.file_history import record_file_save

    @record_file_save(lambda source, target: target, label='Добавление файла в Project Folder')
    def copy(source, target):
        target = Path(target)
        temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.part')
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return copy(source, target)
