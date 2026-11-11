import os
import asyncio
import time
import json
import shutil
import fnmatch
import hashlib
import io
from contextvars import ContextVar
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Dict, Any, Optional

from core.registry import BaseModule, BaseTool
from core.memory import MemoryManager
from core.context import get_execution_context
from core.paths import APP_PATHS
from .file_access import ProjectFiles

memory_manager = MemoryManager()
_file_revisions = ContextVar('file_revisions', default=None)


def _revision(path):
    try:
        return hashlib.sha256(ProjectFiles(get_execution_context().workspace_root).read_bytes(path)).digest()
    except FileNotFoundError:return None


def _remember_file(path, raw):
    revisions = _file_revisions.get()
    if revisions is not None:
        revisions[str(path)] = hashlib.sha256(raw).digest() if raw is not None else None


def _check_file_revision(path):
    revisions = _file_revisions.get()
    if revisions is None:return
    key = str(path)
    current = _revision(path)
    if key not in revisions:
        if current is None:return
        raise RuntimeError('Read the file with read_file before overwriting it. Another agent may be editing it.')
    if revisions[key] != current:
        raise RuntimeError('File changed since your last read, possibly by another agent. Read it again and revise your edit; do not replay the old overwrite blindly.')


CANVAS_PORT_SIDES = {
    "top", "top_right", "right", "bottom_right",
    "bottom", "bottom_left", "left", "top_left",
}









# ==========================================
# УТИЛИТЫ БЕЗОПАСНОСТИ И ПЕСОЧНИЦЫ
# ==========================================

class FileSecurity:
    @staticmethod
    def resolve_safe_path(workspace_root: str, requested_path: str) -> Path:
        """Каноническая проверка: путь строго внутри workspace_root."""
        root = Path(workspace_root).resolve()
        if os.path.isabs(requested_path):
            target = Path(requested_path).resolve()
        else:
            target = (root / requested_path).resolve()

        if not target.is_relative_to(root):
            raise PermissionError(
                f"Security Error: Доступ запрещен! Путь '{requested_path}' выходит за пределы Project Folder '{workspace_root}'."
            )
        return target

    @staticmethod
    def is_binary_file(filepath: Path) -> bool:
        """Проверяет, является ли файл бинарным."""
        try:
            import codecs
            chunk = ProjectFiles(get_execution_context().workspace_root).read_bytes(filepath,1024)
            return b"\x00" in chunk and not chunk.startswith((codecs.BOM_UTF16_LE,codecs.BOM_UTF16_BE,codecs.BOM_UTF32_LE,codecs.BOM_UTF32_BE))
        except Exception:
            return True

    @staticmethod
    def create_backup(target_file: Path):
        """Создает теневой бекап файла в папку сессии перед изменением."""
        if not target_file.exists() or not target_file.is_file():
            return

        backup_dir = (get_execution_context().storage_dir / "file_backups").resolve()
        backup_dir.mkdir(parents=True, exist_ok=True)

        filename = target_file.name
        timestamp = time.time_ns()
        backup_path = backup_dir / f"{filename}_{timestamp}.bak"
        with ProjectFiles(get_execution_context().workspace_root).open(target_file) as source, backup_path.open("wb") as backup:
            shutil.copyfileobj(source,backup,1024*1024)


# ==========================================
# БАЗОВЫЙ КЛАСС ДЛЯ ФАЙЛОВЫХ ИНСТРУМЕНТОВ
# ==========================================

class ContextFileTool(BaseTool):
    def get_sources(self, args, output, workspace):
        if self.name not in {'read_file', 'load_file_to_temp_memory'} or str(output).startswith('System Error'):
            return []
        try:
            from .documents.access import resolve_read_path
            path = resolve_read_path(str(workspace), str(args.get('path') or ''))
        except (OSError, ValueError):return []
        return [{'kind':'file','url':path.as_uri(),'path':str(path),'label':str(args.get('path'))}]

    def history_paths(self, args, context):
        if self.name not in {'write_file','edit_file_block'}:return []
        context = context or get_execution_context()
        return [context.workspace_root / str(args.get('path') or '')]

    def __init__(self, name: str, description: str, args_schema: Dict[str, Any]):
        super().__init__(name=name, description=description, args_schema=args_schema)
    @property
    def workspace_root(self) -> str:
        return str(get_execution_context().workspace_root)

    @property
    def session_id(self) -> str:
        return get_execution_context().safe_session_id


# ==========================================
# ИНСТРУМЕНТЫ МОДУЛЯ
# ==========================================

class FindFilesTool(ContextFileTool):
    history_read_only = True
    def __init__(self):
        super().__init__(
            name="find_files",
            description=(
                "Recursively searches the entire Project Folder for files matching a pattern or name "
                "(e.g. '*.py', '*config*', 'server.js', '*.json'). Ignores heavy folders (.git, node_modules, venv)."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "File search pattern or filename (e.g. 'server.js', '*.ts', '*env*')."
                    },
                    "path": {
                        "type": "string",
                        "description": "Optional subdirectory to start search from. Default is '.' (entire project).",
                        "default": "."
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum matching files to return. Default is 30.",
                        "default": 30
                    }
                },
                "required": ["pattern"]
            }
        )

    async def execute(self, pattern: str, path: str = ".", max_results: int = 30, **kwargs) -> str:
        try:
            target_dir = FileSecurity.resolve_safe_path(self.workspace_root, path)
            if not target_dir.exists() or not target_dir.is_dir():
                return (
                    f"System Error: Starting directory '{path}' does not exist in Project Folder. "
                    "STOP searching: do not repeat scans for this path."
                )

            ignored_dirs = {".git", ".venv", "venv", "__pycache__", ".godot", ".import", "node_modules", "dist", "build"}
            matched_files = []
            root_path = Path(self.workspace_root).resolve()

            files=ProjectFiles(self.workspace_root);pending=[target_dir]
            while pending and len(matched_files)<max_results:
                directory=pending.pop()
                try:entries=files.entries(directory)
                except (FileNotFoundError,NotADirectoryError):continue
                for filename,is_directory,_ in entries:
                    candidate=directory/filename
                    if is_directory:
                        if filename not in ignored_dirs and not filename.startswith('.'):pending.append(candidate)
                    elif fnmatch.fnmatch(filename.lower(),pattern.lower()):
                        matched_files.append(str(candidate.relative_to(root_path)))
                        if len(matched_files)>=max_results:break

            if not matched_files:
                return (
                    f"System: No files matching '{pattern}' were found in '{path}'. "
                    f"STOP SEARCHING: The file does not exist anywhere in this directory tree. "
                    f"Do not call directory tools repeatedly. Either create this file using 'write_file' or inform the user."
                )

            results_text = "\n".join(f"- {f}" for f in matched_files)
            return f"Found {len(matched_files)} file(s) matching '{pattern}':\n{results_text}"

        except Exception as e:
            return f"System Error during search: {e}"


class ListDirectoryTool(ContextFileTool):
    def __init__(self):
        super().__init__(
            name="list_directory",
            description="Lists subdirectories and files in a specific directory. Directories have a trailing slash.",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (e.g. '.' or 'src'). Default is '.'", "default": "."}
                }
            }
        )

    async def execute(self, path: str = ".", **kwargs) -> str:
        try:
            target_dir = FileSecurity.resolve_safe_path(self.workspace_root, path)
            if not target_dir.exists() or not target_dir.is_dir():
                return (
                    f"System Error: Directory '{path}' does not exist in Project Folder. "
                    f"STOP: Do not attempt to list this directory again."
                )

            items = []
            for entry,is_directory,size in ProjectFiles(self.workspace_root).entries(target_dir):
                if entry.startswith('.'):continue
                if is_directory:items.append(f"  {entry}/")
                else:
                    size_str=f"{size} B" if size<1024 else f"{size/1024:.1f} KB"
                    items.append(f"  {entry} ({size_str})")

            display_path = path if path != "." else "/"
            return f"Contents of '{display_path}':\n" + ("\n".join(items) if items else "  (empty directory)")
        except Exception as e:
            return f"System Error: {e}"


class DirectoryTreeTool(ContextFileTool):
    history_read_only = True
    def __init__(self):
        super().__init__(
            name="directory_tree",
            description="Returns a compact, indented tree of all files and folders in Project Folder.",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Starting directory path. Default is '.'", "default": "."},
                    "max_depth": {"type": "integer", "description": "Maximum folder depth to scan. Default is 3.", "default": 3}
                }
            }
        )

    async def execute(self, path: str = ".", max_depth: int = 3, **kwargs) -> str:
        try:
            target_dir = FileSecurity.resolve_safe_path(self.workspace_root, path)
            if not target_dir.exists() or not target_dir.is_dir():
                return (
                    f"System Error: Directory '{path}' not found. "
                    "STOP: Do not repeat directory scans for non-existent paths."
                )

            ignored_dirs = {".git", ".venv", "venv", "__pycache__", ".godot", ".import", "node_modules", "dist", "build"}
            tree_lines = []

            def scan(current_dir: Path, current_depth: int):
                if current_depth > max_depth:
                    return
                indent = "  " * current_depth
                try:entries=ProjectFiles(self.workspace_root).entries(current_dir)
                except (FileNotFoundError,NotADirectoryError):return
                for entry,is_directory,_ in entries:
                    if entry in ignored_dirs or entry.startswith('.'):continue
                    full_path=current_dir/entry
                    if is_directory:
                        tree_lines.append(f"{indent}{entry}/");scan(full_path,current_depth+1)
                    else:tree_lines.append(f"{indent}{entry}")

            scan(target_dir, 0)
            return f"Directory Tree for '{path}':\n" + ("\n".join(tree_lines) if tree_lines else "  (empty)")
        except Exception as e:
            return f"System Error: {e}"


class ReadFileTool(ContextFileTool):
    def __init__(self):
        super().__init__(
            name="read_file",
            description="Reads text/source files with optional line ranges. For PDF, Office, EPUB, HTML or spreadsheets, use inspect_document/read_document/read_spreadsheet.",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Project-relative path, or attachment://att_ID for an explicitly attached text/source file."},
                    "start_line": {"type": "integer", "description": "Optional 1-based start line number."},
                    "end_line": {"type": "integer", "description": "Optional 1-based end line number."}
                },
                "required": ["path"]
            }
        )

    async def execute(self, path: str, start_line: Optional[int] = None, end_line: Optional[int] = None, **kwargs) -> str:
        try:
            from .documents.access import resolve_read_path, read_bytes
            target_file = resolve_read_path(self.workspace_root, path)
            from .documents import DOCUMENT_SUFFIXES
            if target_file.suffix.lower() in DOCUMENT_SUFFIXES - {'.html', '.htm', '.csv', '.tsv', '.rtf'}:
                return f"System: Use inspect_document for '{path}', then read_document, search_document or read_spreadsheet. Use render_document for page images. Original file: {target_file}"
            if not target_file.exists() or not target_file.is_file():
                _remember_file(target_file, None)
                return (
                    f"System Error: File '{path}' does NOT exist in Project Folder. "
                    f"STOP SEARCHING: Do not repeatedly list directories looking for it. "
                    f"If this file is required, create it using 'write_file', or inform the user that it does not exist."
                )

            sample = await asyncio.to_thread(read_bytes, self.workspace_root, str(target_file), 1024)
            import codecs
            if b'\x00' in sample and not sample.startswith((codecs.BOM_UTF16_LE,codecs.BOM_UTF16_BE,codecs.BOM_UTF32_LE,codecs.BOM_UTF32_BE)):
                return f"System Error: Cannot read '{path}'. It is a binary file."
            raw = await asyncio.to_thread(read_bytes, self.workspace_root, str(target_file))
            from core.text_files import decode_text
            text,_=decode_text(raw)
            _remember_file(target_file, raw)
            lines = io.StringIO(text, newline=None).readlines()

            total_lines = len(lines)
            s_idx = max(0, (start_line - 1)) if start_line else 0
            e_idx = min(total_lines, end_line) if end_line else total_lines

            content = "".join(lines[s_idx:e_idx])
            range_info = f" (lines {s_idx+1}-{e_idx} of {total_lines})" if (start_line or end_line) else ""
            return f"File '{path}'{range_info}:\n{content}"
        except Exception as e:
            return f"System Error: {e}"


class WriteFileTool(ContextFileTool):
    def __init__(self):
        super().__init__(
            name="write_file",
            description="Writes content to a file. Automatically creates parent directories and makes shadow backups.",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
                    "content": {"type": "string", "description": "Complete text content to write."}
                },
                "required": ["path", "content"]
            }
        )

    async def execute(self, path: str, content: str, **kwargs) -> str:
        try:
            target_file = FileSecurity.resolve_safe_path(self.workspace_root, path)

            _check_file_revision(target_file)
            is_new = not target_file.exists()
            if not is_new:
                FileSecurity.create_backup(target_file)

            ProjectFiles(self.workspace_root).write_text(target_file,content)
            _remember_file(target_file,ProjectFiles(self.workspace_root).read_bytes(target_file))

            if is_new:
                return f"System: New file '{path}' created and saved successfully."
            else:
                return f"System: Existing file '{path}' overwritten successfully (shadow backup created)."
        except Exception as e:
            return f"System Error writing to '{path}': {e}"


class EditFileBlockTool(ContextFileTool):
    def __init__(self):
        super().__init__(
            name="edit_file_block",
            description=(
                "Surgically replaces a specific unique block of text (old_text) with new_text. "
                "Auto-creates backup."
            ),
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
                    "old_text": {"type": "string", "description": "The exact existing text block to be replaced."},
                    "new_text": {"type": "string", "description": "The new replacement text block."}
                },
                "required": ["path", "old_text", "new_text"]
            }
        )

    async def execute(self, path: str, old_text: str, new_text: str, **kwargs) -> str:
        try:
            target_file = FileSecurity.resolve_safe_path(self.workspace_root, path)
            if not target_file.exists() or not target_file.is_file():
                return (
                    f"System Error: File '{path}' does NOT exist. Cannot edit. "
                    f"Use 'write_file' if you want to create a new file."
                )

            _check_file_revision(target_file)
            from core.text_files import decode_text
            file_content = decode_text(ProjectFiles(self.workspace_root).read_bytes(target_file))[0]

            count = file_content.count(old_text)
            if count == 0:
                return (
                    f"System Error: 'old_text' was not found in '{path}'. "
                    f"Use 'read_file' to inspect the exact lines before editing."
                )
            if count > 1:
                return (
                    f"System Error: 'old_text' matches {count} locations in '{path}'. "
                    f"Please provide a larger unique block with surrounding lines."
                )
            updated_content = file_content.replace(old_text, new_text, 1)

            FileSecurity.create_backup(target_file)

            ProjectFiles(self.workspace_root).write_text(target_file,updated_content)
            _remember_file(target_file,ProjectFiles(self.workspace_root).read_bytes(target_file))

            return f"System: File '{path}' successfully edited (block replaced, backup saved)."
        except Exception as e:
            return f"System Error: {e}"


class LoadFileToTempMemoryTool(ContextFileTool):
    def __init__(self):
        super().__init__(
            name="load_file_to_temp_memory",
            description="Loads a heavy local file into temporary memory (paginated or vectorized). Useful for huge logs or manuals.",
            args_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the local file."}
                },
                "required": ["path"]
            }
        )

    async def execute(self, path: str, **kwargs) -> str:
        try:
            target_file = FileSecurity.resolve_safe_path(self.workspace_root, path)
            if not target_file.exists() or not target_file.is_file():
                return f"System Error: File '{path}' not found in Project Folder."

            if FileSecurity.is_binary_file(target_file):
                return f"System Error: Cannot load binary file '{path}' to memory."

            from core.text_files import decode_text
            content = decode_text(await asyncio.to_thread(ProjectFiles(self.workspace_root).read_bytes,target_file))[0]

            temp_id = await memory_manager.save_temp_data(content=content, source=f"local_file: {path}")
            return (
                f"System: File '{path}' ({len(content)} chars) loaded into temporary memory under ID: '{temp_id}'. "
                "Use 'search_temp_memory' to search the active temporary-memory database."
            )
        except Exception as e:
            return f"System Error: {e}"


# ==========================================
# ИНИЦИАЛИЗАЦИЯ МОДУЛЯ
# ==========================================

class Module(BaseModule):
    def dispatch_scope(self, context, tools, messages, emit, *, call_id=""):
        from .documents.access import attachment_scope
        return attachment_scope(context, messages)

    @asynccontextmanager
    async def execution_context_scope(self, context, tools):
        token = _file_revisions.set({})
        try:yield
        finally:_file_revisions.reset(token)

    def __init__(self):
        from .documents.tools import tools as document_tools
        self.tools = [
            FindFilesTool(),
            ListDirectoryTool(),
            DirectoryTreeTool(),
            ReadFileTool(),
            WriteFileTool(),
            EditFileBlockTool(),
            LoadFileToTempMemoryTool()
        ] + document_tools()

    def get_tools(self) -> List[BaseTool]:
        return self.tools

    def get_system_prompt_addon(self) -> str:
        return (
            "You have access to the project file system through the 'file_manager' module:\n"
            "- 'find_files': fast recursive file search by mask (for example '*.py', '*config*', 'server.js'). Use it first when locating files instead of manually traversing directories.\n"
            "- 'list_directory' and 'directory_tree': inspect directory contents.\n"
            "- 'read_file': read complete files or line ranges.\n"
            "- Documents and spreadsheets: use 'inspect_document' first, then 'read_document', 'search_document' or 'read_spreadsheet' for only the needed sections/cell ranges. Follow continuation cursors.\n"
            "- 'render_document': request selected page images or embedded images when visual layout matters and the model supports vision. Use OCR in read_document for scans when vision is unavailable.\n"
            "- Document tool results include the original path and workspace_path for terminal operations. For Docker/SSH use the terminal attachment mapping.\n"
            "- 'write_file': create files or fully overwrite them (with automatic backups).\n"
            "- 'edit_file_block': replace a specific existing block. \n"
            "- Before overwriting or editing an existing file, read it with read_file in this run. If it changed, re-read and reconsider the edit. Parallel agents share Project Folder.\n"
            "- RULE: If 'find_files' or 'read_file' reports that a file does not exist, do not repeatedly rescan directories for the same missing file. Accept that it is absent.\n\n"

        )
