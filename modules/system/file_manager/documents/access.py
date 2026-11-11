"""Only explicit attachments may be read outside the project directory."""
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit
from core.attachments import resolve_attachment_path

_authorized=ContextVar('document_attachments',default=None)


def authorized():return _authorized.get() or {}


def resolve_read_path(workspace, requested):
    """Resolve only a project path or an exact, request-scoped attachment alias."""
    value = str(requested).strip()
    grants = authorized()
    if value in grants:
        return Path(grants[value]['_host_path'])
    if value.startswith('file:'):
        url = urlsplit(value)
        if url.netloc not in ('', 'localhost'):
            raise PermissionError('Remote file URLs are not allowed.')
        value = unquote(url.path)
        if re.match(r'^/[A-Za-z]:/', value):value = value[1:]
    root = Path(workspace).resolve()
    target = (root / value).resolve()
    if target.is_relative_to(root) or str(target) in grants:
        return target
    raise PermissionError('File must be inside Project Folder or explicitly attached to this conversation. '
                          'Use the attachment:// ID supplied in the attachment notice.')


def read_bytes(workspace, path, limit=None):
    from ..file_access import ProjectFiles
    target = resolve_read_path(workspace, path)
    root = Path(workspace).resolve()
    # ProjectFiles pins directories and refuses symlinks. The external-file
    # grant applies to this file only; its parent never becomes a model root.
    return ProjectFiles(root if target.is_relative_to(root) else target.parent).read_bytes(target, limit)


@contextmanager
def attachment_scope(context,messages):
    paths={}
    for message in messages:
        for item in (getattr(message,'additional_kwargs',{}) or {}).get('attachments') or []:
            if not isinstance(item,dict) or item.get('memory_only') or item.get('temp_memory'):continue
            try:path=resolve_attachment_path(item,context.session_id).resolve()
            except (OSError,ValueError,RuntimeError):continue
            grant = {**item, '_host_path': str(path)}
            paths[str(path)] = grant
            identity = str(item.get('id') or '')
            if re.fullmatch(r'att_[a-f0-9]{32}', identity):
                paths['attachment://' + identity] = grant
                # Older model requests already contain these Docker paths.
                # Match the complete generated name, never a basename/suffix.
                filename = path.name
                paths['/home/toshy/uploads/' + identity + '/' + filename] = grant
                paths['attachments/' + identity + '/' + filename] = grant
    token=_authorized.set(paths)
    try:yield
    finally:_authorized.reset(token)
