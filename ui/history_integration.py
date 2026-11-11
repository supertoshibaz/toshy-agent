"""History navigation and editor coordination, without changing chat ownership."""
from ui.styles.binding import apply_style
from ui.i18n import bind_caption, tr, set_text

from pathlib import Path
from ui.widgets.dialogs import MessageBox as QMessageBox


def prepare_restore(window, paths):
    selected={str(Path(p).resolve()) for p in paths}
    text=getattr(window,'file_view',None)
    board=getattr(window,'board_view',None)
    paint=getattr(window,'paint_view',None)
    blocked=[]
    for document in getattr(text,'open_docs',{}).values():
        if str(Path(document.file_path).resolve()) in selected and document.is_dirty:
            blocked.append(document.file_path+tr('ui.caption.16d85e3d22d0'))
    for document in getattr(board,'open_boards',{}).values():
        if str(Path(document.file_path).resolve()) in selected and document.is_dirty:
            blocked.append(document.file_path+tr('ui.caption.0e8bf86c9458'))
    for document in getattr(paint,'documents',[]):
        path=getattr(document,'file_path','')
        if path and str(Path(path).resolve()) in selected:
            blocked.append(path+tr('ui.caption.b7db1a20abd5'))
    if blocked:
        QMessageBox.warning(window,tr('ui.history_integration.restore_files_5349'),
            tr('ui.caption.55cb8202101d')+'\n'.join(blocked))
        return False
    return True


def finish_restore(window,paths):
    selected={str(Path(p).resolve()) for p in paths}
    text=getattr(window,'file_view',None)
    board=getattr(window,'board_view',None)
    for document in list(getattr(text,'open_docs',{}).values()):
        if str(Path(document.file_path).resolve()) not in selected:continue
        if Path(document.file_path).is_file():
            if hasattr(document,'load_file_content'):document.load_file_content()
        else:
            for state in text.project_tabs.states.values():
                state['open_files']=[p for p in state.get('open_files',[]) if p!=document.file_path]
                state['tabs']=[entry for entry in state.get('tabs',[]) if entry.get('key')!=document.file_path]
            text._close_tab_by_path(document.file_path)
    for document in list(getattr(board,'open_boards',{}).values()):
        if str(Path(document.file_path).resolve()) not in selected:continue
        if Path(document.file_path).is_file():
            if document.canvas is not None:
                document._auto_save_timer.stop()
                blocked=document.canvas.blockSignals(True)
                try:
                    document.canvas.load_from_file(document.file_path)
                    document.canvas.undo_stack.clear()
                finally:document.canvas.blockSignals(blocked)
                document._auto_save_timer.stop();document.is_dirty=False
                document.dirty_state_changed.emit(False)
        else:board._close_tab_by_path(document.file_path)


def open_history(window,project='',path='',session='',run=''):
    view=getattr(window,'projects_view',None)
    if view is None:return
    from ui.app_state import app_state
    if path and not project:
        target=Path(path).resolve()
        candidates=[(identity,record.get('workspace')) for identity,record in app_state.projects.items()
                    if record.get('workspace') and target.is_relative_to(Path(record['workspace']).resolve())]
        if candidates:
            project=max(candidates,key=lambda pair:len(Path(pair[1]).parts))[0]
    project=project or app_state.project_id
    if not project:return
    view._select_entity('folder',project)
    view._selected_type,view._selected_id='folder',project
    view._restore_details()
    view.folder_tabs.setCurrentWidget(view.history)
    # Chat links select a run in the common timeline. A hidden run/session
    # filter made each subsequent click appear to replace the whole history.
    view.history._reset_filters()
    if path:
        try:path=Path(path).resolve().relative_to(Path(app_state.projects.get(project,{}).get('workspace') or app_state.workspace).resolve()).as_posix()
        except ValueError:pass
        bind_caption(view.history.search, path)
    view.history.focus_run=run
    view.history._filter()
    view.history.refresh()
    from ui.event_bus import event_bus
    event_bus.switch_view_tab_requested.emit(1)


def refresh_chat_history(view):
    from shiboken6 import isValid
    from ui.views.chat_view import AssistantMessageTurn, ThoughtMessageTurn
    from ui.app_state import app_state
    from ui.core_bridge import core_process,SessionApiClient
    from ui.core_bridge.async_runner import AsyncRunner
    from PySide6.QtWidgets import QPushButton
    from PySide6.QtCore import Qt
    project=app_state.session_projects.get(view.current_session_id) or app_state.project_id
    turns=[view.turns_layout.itemAt(i).widget() for i in range(view.turns_layout.count())]
    candidates=[turn for turn in turns if isinstance(turn,(AssistantMessageTurn,ThoughtMessageTurn)) and getattr(turn,'_run_state',{}).get('run_id')]
    targets={}
    for turn in candidates:
        run=turn._run_state['run_id']
        if run not in targets or isinstance(turn,ThoughtMessageTurn):targets[run]=turn
    eligible=list(targets.values())
    for turn in candidates:
        chip = getattr(turn, 'file_history_btn', None)
        if turn not in eligible and chip is not None and isValid(chip):chip.hide()
    signature=(view.current_session_id,view.current_branch if hasattr(view,'current_branch') else '',
               tuple((id(t),t._run_state.get('run_id'),t._run_state.get('status')) for t in eligible))
    if not project or not eligible or getattr(view,'_history_signature',None)==signature or not core_process.is_running():return
    view._history_signature=signature
    if not hasattr(view,'_history_runner'):view._history_runner=AsyncRunner(view,max_threads=1)
    session=view.current_session_id
    client=SessionApiClient(base_url=core_process.base_url)
    def loaded(value):
        if view.current_session_id!=session:return
        changes={}
        for row in value['items']:
            if row['kind'] in {'before','before_run','baseline'}:continue
            changes.setdefault(row['run'],set()).update(x['path'] for x in row['files'])
        for turn in eligible:
            if not isValid(turn):
                continue
            try:
                run=turn._run_state['run_id'];count=len(changes.get(run,set()))
                chip = getattr(turn, 'file_history_btn', None)
                if chip is None or not isValid(chip):
                    button=turn.file_history_btn=QPushButton(turn)
                    button.clicked.connect(lambda _=False,r=run:open_history(view.window(),project=project,session=session,run=r))
                    if isinstance(turn,ThoughtMessageTurn):
                        turn.body_layout.addWidget(button,0,Qt.AlignLeft)
                    else:
                        actions=getattr(turn,'actions_bar',None)
                        index=turn.main_layout.indexOf(actions) if actions is not None else -1
                        if index>=0:turn.main_layout.insertWidget(index,button,0,Qt.AlignLeft)
                        else:turn.main_layout.addWidget(button,0,Qt.AlignLeft)
                    button.setMinimumHeight(26)
                    button.setCursor(Qt.PointingHandCursor)
                    apply_style(button, 'QPushButton {background: @background.panel@; color: @text.secondary@; border: 1px solid @border.subtle@; border-radius: 8px; padding: 4px 8px; font-size: 11px;} QPushButton:hover {border-color: @accent.primary@; color: white;}')
                set_text(turn.file_history_btn, 'ui.history_integration.files_changed_v0_8ae8', v0=count)
                bind_caption(turn.file_history_btn, '\n'.join(sorted(changes.get(run,set()))), setter='setToolTip')
                turn.file_history_btn.setVisible(count>0)
            except RuntimeError:pass
    view._history_runner.submit(lambda:client._request('POST',f'/v1/file-history/{project}/list',json={'session':session},timeout=60),loaded,
                               lambda _:setattr(view,'_history_signature',None))
