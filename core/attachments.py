from __future__ import annotations

import base64
import mimetypes
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from core.paths import APP_PATHS


class AttachmentError(RuntimeError):
    """Raised when a chat attachment cannot be stored, read, or transported."""


_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".py", ".pyi", ".js", ".mjs", ".cjs",
    ".ts", ".tsx", ".jsx", ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".conf", ".csv", ".tsv", ".xml", ".html", ".htm", ".css", ".scss",
    ".sass", ".less", ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    ".c", ".h", ".cpp", ".hpp", ".cc", ".cxx", ".rs", ".go", ".java", ".kt",
    ".kts", ".cs", ".rb", ".php", ".swift", ".sql", ".r", ".lua", ".pl", ".pm",
    ".tex", ".log", ".env", ".properties", ".gradle", ".vue", ".svelte", ".dockerignore",
    ".gitignore", ".gitattributes", ".editorconfig", ".make", ".mk",
}

_UNSUPPORTED_DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".pages", ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp", ".epub", ".mobi", ".rtf",
}

_TEXT_FILENAMES = {
    "dockerfile", "makefile", "cmakelists.txt", "license", "readme", "authors", "copying",
}


def _safe_filename(name: str) -> str:
    raw = Path(name or "attachment").name
    cleaned = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", raw).strip().strip(".")
    return cleaned or "attachment"


def _looks_like_text(path: Path, sample_size: int = 65536) -> bool:
    try:
        with path.open('rb') as stream:
            sample = stream.read(sample_size)
    except OSError:
        return False
    if not sample:
        return True

    # BOM-marked Unicode text can legitimately contain many NUL bytes.
    for bom in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff", b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"):
        if sample.startswith(bom):
            return True

    # A NUL byte in an unmarked stream is a strong binary-file signal.
    if b"\x00" in sample:
        return False

    # Unknown-extension source/config files are often legacy Windows text.
    # Try common encodings, but still require the decoded sample to look like
    # human-readable text so arbitrary binaries do not become attachments.
    for encoding in ("utf-8", "cp1251", "cp1252"):
        try:
            decoded = sample.decode(encoding)
        except UnicodeDecodeError:
            continue
        if not decoded:
            return True
        controls = sum(1 for ch in decoded if ord(ch) < 32 and ch not in "\n\r\t\f\b")
        printable = sum(1 for ch in decoded if ch.isprintable() or ch in "\n\r\t\f\b")
        if controls / max(1, len(decoded)) < 0.01 and printable / max(1, len(decoded)) > 0.90:
            return True
    return False


def inspect_attachment_path(path: str | os.PathLike[str]) -> Dict[str, Any]:
    source = Path(path).expanduser()
    try:
        source = source.resolve(strict=True)
    except OSError as error:
        raise AttachmentError(f"Файл не найден: {source}") from error
    if not source.is_file():
        raise AttachmentError(f"Можно прикреплять только файлы: {source}")

    mime_type = mimetypes.guess_type(str(source))[0] or "application/octet-stream"
    lower_mime = mime_type.lower()
    suffix = source.suffix.lower()
    filename_key = source.name.lower()

    if lower_mime.startswith("image/"):
        media_type = "image"
    elif lower_mime.startswith("audio/"):
        media_type = "audio"
    elif lower_mime.startswith("video/"):
        media_type = "video"
    elif suffix in _UNSUPPORTED_DOCUMENT_EXTENSIONS:
        media_type = 'file'
    elif (
        lower_mime.startswith("text/")
        or suffix in _TEXT_EXTENSIONS
        or filename_key in _TEXT_FILENAMES
        or _looks_like_text(source)
    ):
        media_type = "text"
        if lower_mime == "application/octet-stream":
            mime_type = "text/plain"
    else:
        media_type = 'file'

    return {
        "filename": source.name,
        "mime_type": mime_type,
        "media_type": media_type,
        "size": int(source.stat().st_size),
        "source_path": str(source),
    }


def resolve_attachment_path(attachment: Dict[str, Any], session_id: Optional[str]) -> Path:
    raw_path = str(attachment.get("path") or "").strip()
    if not raw_path:
        raise AttachmentError(f"У вложения '{attachment.get('filename', 'file')}' отсутствует путь.")

    storage = str(attachment.get("storage") or "copy").lower()
    path = Path(raw_path).expanduser()
    if storage == "link" or path.is_absolute():
        resolved = path.resolve()
    else:
        if not session_id:
            raise AttachmentError(
                f"Нельзя разрешить путь вложения '{attachment.get('filename', 'file')}' без session_id."
            )
        resolved = (APP_PATHS.sessions / session_id / path).resolve()

    if not resolved.exists() or not resolved.is_file():
        raise AttachmentError(
            f"Файл вложения недоступен: {attachment.get('filename', resolved.name)}\nПуть: {resolved}"
        )
    return resolved


def read_text_attachment(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise AttachmentError(f"Не удалось прочитать текстовый файл '{path.name}': {error}") from error

    # The common encodings first; legacy Windows encodings are useful for old source/config files.
    for encoding in ("utf-8-sig", "utf-16", "utf-32", "cp1251", "cp1252"):
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, UnicodeError):
            continue
    raise AttachmentError(
        f"Не удалось определить текстовую кодировку файла '{path.name}'. "
        "Сейчас поддерживаются UTF-8/16/32 и распространённые Windows-кодировки."
    )


def file_to_data_url(path: Path, mime_type: str) -> str:
    try:
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as error:
        raise AttachmentError(f"Не удалось прочитать вложение '{path.name}': {error}") from error
    return f"data:{mime_type};base64,{encoded}"


def file_to_base64(path: Path) -> str:
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as error:
        raise AttachmentError(f"Не удалось прочитать вложение '{path.name}': {error}") from error


__all__ = [
    "AttachmentError",
    "inspect_attachment_path",
    "resolve_attachment_path",
    "read_text_attachment",
    "file_to_data_url",
    "file_to_base64",
    "_safe_filename",
]
