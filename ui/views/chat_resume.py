"""Card-local continuation controls; checkpoint ownership remains in the core."""
from ui.i18n import bind_caption, tr, localize

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QPushButton


PAUSED_STATUSES = {'stopped', 'interrupted', 'unavailable'}


def install_controls(view, turn):
    if not hasattr(turn, 'continue_btn'):
        turn.continue_btn = localize(QPushButton('', turn), 'ui.chat_resume.continue_a42a')
        turn.continue_btn.setObjectName('ChatContinueBtn')
        turn.continue_btn.setCursor(Qt.PointingHandCursor)
        turn.main_layout.addWidget(turn.continue_btn, 0, Qt.AlignLeft)
        turn.continue_btn.clicked.connect(lambda: view._resume_chat_turn(turn))
        turn.continue_btn.hide()
    return turn


def restore_controls(view, kwargs, start_index):
    state = kwargs.get('run_state')
    if not state:
        return
    for index in range(start_index, view.turns_layout.count() - 1):
        turn = view.turns_layout.itemAt(index).widget()
        if hasattr(turn, 'main_layout'):
            install_controls(view, turn)
            turn._run_state = dict(state)
    refresh_controls(view)


def refresh_controls(view):
    from ui.views.chat_steering import refresh_update_actions
    refresh_update_actions(view)
    from ui.history_integration import refresh_chat_history
    refresh_chat_history(view)
    from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn, AssistantMessageTurn
    turns = [view.turns_layout.itemAt(i).widget() for i in range(view.turns_layout.count())]
    last_user = max((i for i, turn in enumerate(turns) if isinstance(turn, UserMessageTurn) and not getattr(turn, '_steering_message', None)), default=-1)
    eligible = []
    for index, turn in enumerate(turns):
        if isinstance(turn, ThoughtMessageTurn):turn.sync_activity()
        if hasattr(turn, 'continue_btn'):
            turn.continue_btn.hide()
        state = getattr(turn, '_run_state', {})
        if index > last_user and isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn)) and state.get('status') in PAUSED_STATUSES:
            eligible.append(turn)
    if not eligible:
        return
    state = eligible[-1]._run_state
    target = next((t for t in eligible if t.chat_meta.get('chat_id') == state.get('target_chat_id')), eligible[-1])
    install_controls(view, target)
    bind_caption(target.continue_btn, state.get('error') or (tr('ui.caption.3028fa0907c0') if state.get('status') == 'stopped' else tr('ui.caption.4a798ae4d374')), setter='setToolTip')
    target.continue_btn.setEnabled(not bool(view._active_runs.get(view._current_run_key())))
    target.continue_btn.show()


def mark_run(view, run_id, state):
    from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn, AssistantMessageTurn
    turns = [view.turns_layout.itemAt(i).widget() for i in range(view.turns_layout.count())]
    last_user = max((i for i, turn in enumerate(turns) if isinstance(turn, UserMessageTurn) and not getattr(turn, '_steering_message', None)), default=-1)
    for turn in turns[last_user + 1:]:
        if isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn)):
            install_controls(view, turn)
            turn._run_state = {**getattr(turn, '_run_state', {}), **state, 'run_id': run_id}
            if state.get('status') in PAUSED_STATUSES and isinstance(turn, ThoughtMessageTurn):
                turn.pause_tools()
    refresh_controls(view)
