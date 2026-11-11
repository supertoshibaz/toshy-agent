"""One application-wide text tab set, including migration of old project lists."""
from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import QObject
from ui.tab_memory import SuspendedTab


class ProjectTextTabs(QObject):
    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.current = '_default'
        self.states = {}
        self.migrate_legacy = False

    def capture(self):
        view = self.view
        entries, files, states = [], [], {}
        for index in range(view.tab_bar.count()):
            document = view.tab_bar.tabData(index)
            path = getattr(document, 'file_path', None)
            if path and path in view.open_docs:
                entries.append({'kind':'file', 'key':path})
                files.append(path)
                states[path] = deepcopy(document.saved_state if isinstance(document, SuspendedTab)
                                         else view.tab_memory._snapshot(document))
            else:
                key = next((key for key, item in view.virtual_docs.items() if item is document), None)
                if key:
                    entry = {'kind':'virtual', 'key':key, 'title':document.title}
                    if not key.startswith('builtin_doc:'):
                        source = document._edit_document
                        entry['document'] = {
                            'content': source.editor.toPlainText() if source is not None else document.editor.toPlainText(),
                            'original': getattr(source, '_memory_content', None) if getattr(source, '_memory_only', False) else None,
                            'breadcrumb': document.path_label.text(),
                            'base_path': str(document._base_path or ''),
                            'editing': document._editing,
                            'modified': document.is_dirty,
                        }
                    entries.append(entry)
        return {'open_files':files, 'active_file_index':view.tab_bar.currentIndex(),
                'file_states':states, 'tabs':entries}

    def switch(self, project):
        # Text tabs are application-wide; project selection only changes roots
        # used when opening/creating files, never the current editor buffers.
        self.current = '_global'
        self.migrate_legacy = False
        view = self.view
        view._on_current_tab_changed(view.tab_bar.currentIndex())

    def restore(self, states, project='', *, legacy=False):
        self.current = '_global'
        self.states = {}
        # ProjectWorkspaces still uses this one-shot flag to restore old folder
        # bindings; it no longer determines which text tabs are displayed.
        self.migrate_legacy = bool(legacy)
        states = deepcopy(states or {})
        selected = str(project or '_default')
        if '_global' in states:
            ordered = [states['_global']]
        else:
            # Merge legacy project lists once, preserving the selected project's
            # order/active file and keeping every unique open file accessible.
            preferred = states.get(selected) or states.get('_default') or {}
            ordered = [preferred, *[state for state in states.values() if state is not preferred]]
        view = self.view
        seen = set()
        preferred_entries = ordered[0].get('tabs') or [
            {'kind':'file','key':path} for path in ordered[0].get('open_files', [])]
        active = int(ordered[0].get('active_file_index', 0))
        active_entry = preferred_entries[active] if 0 <= active < len(preferred_entries) else None
        for state in ordered:
            entries = state.get('tabs') or [{'kind':'file','key':path} for path in state.get('open_files', [])]
            for entry in entries:
                identity = entry['kind'], entry['key']
                if identity in seen:
                    continue
                seen.add(identity)
                path = entry['key']
                if entry['kind'] == 'file' and (path in view.open_docs or Path(path).is_file()):
                    view.open_file(path, force=True)
                    document = view.open_docs.get(path)
                    saved = state.get('file_states', {}).get(path)
                    if saved and document is not None and not isinstance(document, SuspendedTab):
                        view.tab_memory._restore(document, saved)
                elif entry['kind'] == 'virtual':
                    if path.startswith('builtin_doc:'):
                        from ui.builtin_docs import document_path
                        try:
                            filename = path.removeprefix('builtin_doc:')
                            resource = document_path(filename)
                            view.open_temporary_markdown(key=path, title=filename,
                                content=resource.read_text(encoding='utf-8-sig'),
                                breadcrumb='Built-in / ' + filename, base_path=str(resource))
                        except (OSError, UnicodeError, ValueError) as error:
                            from core.logger import core_logger
                            core_logger.warn('DOCUMENT_RESTORE', str(error))
                    elif isinstance(entry.get('document'), dict):
                        saved = entry['document']
                        content = str(saved.get('content') or '')
                        original = saved.get('original')
                        view.open_temporary_markdown(key=path, title=entry.get('title') or path,
                            content=original if isinstance(original, str) else content,
                            breadcrumb=str(saved.get('breadcrumb') or 'Temporary'),
                            base_path=saved.get('base_path') or None)
                        document = view.virtual_docs[path]
                        if (saved.get('editing') or saved.get('modified')) and not document._immutable:
                            document.toggle_editing()
                            document._edit_document.editor.setPlainText(content)
                            document._edit_document.set_dirty(bool(saved.get('modified')))
                            if not saved.get('editing'):
                                document.toggle_editing()
                    elif path in view.virtual_docs:
                        document = view.virtual_docs[path]
                        if not any(view.tab_bar.tabData(i) is document for i in range(view.tab_bar.count())):
                            index = view.tab_bar.addTab(entry.get('title') or document.title)
                            view.tab_bar.setTabData(index, document)
                            view._attach_virtual_close_button(index, path)
        if active_entry:
            for index in range(view.tab_bar.count()):
                document = view.tab_bar.tabData(index)
                path = (getattr(document, 'file_path', None) if active_entry['kind'] == 'file'
                        else next((key for key, value in view.virtual_docs.items() if value is document), None))
                if path == active_entry['key']:
                    view.tab_bar.setCurrentIndex(index)
                    view._on_current_tab_changed(index)
                    break
        view.tab_memory.schedule()

    def export(self):
        return {'_global': self.capture()}

    def referenced_elsewhere(self, kind, key):
        return False

    def rename(self, old, new):
        from os import sep
        def renamed(path):
            return new + path[len(old):] if path == old or path.startswith(old + sep) else path
        for state in self.states.values():
            state['open_files'] = [renamed(path) for path in state.get('open_files', [])]
            state['file_states'] = {renamed(path):value for path,value in state.get('file_states', {}).items()}
            for entry in state.get('tabs', []):
                if entry['kind'] == 'file':
                    entry['key'] = renamed(entry['key'])
