"""Отправка уточнения без второго запроса к модели и без записи GUI поверх журнала ядра."""
from ui.i18n import bind_caption, bind_source, tr, set_text

import uuid
from PySide6.QtCore import Qt
from core.security import auth_headers, load_or_create_core_token
from ui.core_bridge.transport import core_http_client
from ui.core_bridge.process_manager import core_process


def send_update(chat):
    worker = chat.current_worker
    if getattr(chat, '_steering_pending', False):
        return
    text = chat.input_edit.toPlainText().strip()
    sources = [dict(item) for item in chat._pending_attachments]
    if not text and not sources:
        return
    tags = list(chat.tags_bar.get_active_tags())
    reply = chat.chat_references.snapshot_reply()
    skills = sorted(chat.input_controls.skills.selected)
    metadata = {'chat_id': 'm' + uuid.uuid4().hex[:16], 'tags': tags}
    if reply:
        metadata['chat_reply'] = dict(reply)
    if skills:
        metadata['requested_resources'] = {'skills': skills}
    if chat.input_controls.full_web_action.isChecked():
        metadata['full_web_pages'] = True
    payload = {'session_id': worker.session_id, 'branch': worker.branch,
               'message': {'content': text, 'additional_kwargs': metadata}, 'sources': sources}
    run_key = chat._current_run_key()
    chat._steering_pending = True
    chat.send_btn.setEnabled(False)
    def request():
        with core_http_client(core_process.base_url, timeout=120) as client:
            response = client.post(core_process.base_url + '/v1/chat/runs/' + worker.run_id + '/steer',
                json=payload, headers=auth_headers(load_or_create_core_token()))
            if response.is_error:
                try:
                    detail = response.json().get('detail')
                except ValueError:
                    detail = response.text
                raise ValueError(detail or 'Не удалось передать уточнение.')
            return response.json()
    def finish():
        chat._steering_pending = False
        chat.send_btn.setEnabled(True)
    def done(result):
        finish()
        chat._delete_temporary_attachment_sources(sources)
        if run_key != chat._current_run_key():
            return
        # Не стираем текст, который пользователь уже успел дописать после отправки.
        if chat.input_edit.toPlainText().strip() == text:
            chat.input_edit.clear()
        identities = {item.get('pending_id') for item in sources}
        chat._pending_attachments[:] = [item for item in chat._pending_attachments if item.get('pending_id') not in identities]
        chat._delete_temporary_attachment_sources(sources)
        chat._refresh_pending_attachments_panel()
        chat.chat_references.consume_reply(reply)
        chat.input_controls.skills.selected.difference_update(skills)
        chat.input_controls.skills.populate()
        chat.input_controls.skills.changed.emit()
        chat.input_controls.full_web_action.setChecked(False)
    def failed(error):
        finish()
        if run_key == chat._current_run_key():
            chat._show_error_panel(str(error))
    chat.attachment_runner.submit(request, done, failed)


def render_update(chat, event, run_id):
    from ui.views.chat_view import UserMessageTurn
    from core.chat_references import CHAT_ID
    from langchain_core.messages import messages_from_dict
    identity = event.get('id') or event.get('chat_id')
    existing = next((turn for turn in chat.user_turn_widgets if getattr(turn, 'chat_meta', {}).get(CHAT_ID) == identity), None)
    if event.get('type') == 'steering_applied':
        if existing:
            set_text(existing, 'ui.chat_steering.update_sent_to_the_model_a0d3', setter='setToolTip')
        return existing
    if existing:
        return existing
    message = messages_from_dict([event['message']])[0]
    kwargs = dict(message.additional_kwargs)
    turn = UserMessageTurn(text=message.content, tags=kwargs.get('tags') or [],
        attachments=kwargs.get('attachments') or [], attachment_resolver=chat._resolve_attachment_for_ui,
        parent=chat.turns_container)
    turn.chat_meta.update(kwargs)
    turn.delete_requested.connect(chat._on_turn_delete_requested)
    turn.retry_requested.connect(chat._on_turn_retry_requested)
    turn.create_branch_requested.connect(chat._on_create_branch_from_turn)
    turn.content_edited.connect(chat._sync_session_to_disk)
    turn._steering_run_id = run_id
    turn._steering_message = event['message']
    bind_caption(turn, tr('ui.chat_steering.update_sent_to_the_model_a0d3') if event.get('applied') else tr('ui.caption.4dbeaf73db00'), setter='setToolTip')
    turn._steering_action_tooltips = {kind: turn.actions_bar.buttons[kind].toolTip()
        for kind in ('edit', 'retry', 'delete', 'branch', 'forward') if kind in turn.actions_bar.buttons}
    index = chat._generation_insert_index
    if index is None:
        index = chat.turns_layout.count() - 1
    else:
        chat._generation_insert_index += 1
    chat.turns_layout.insertWidget(index, turn)
    chat.user_turn_widgets.append(turn)
    refresh_update_actions(chat)
    chat.timeline_rail.update_turns(chat.user_turn_widgets)
    return turn


def refresh_update_actions(chat):
    active_ids = {worker.run_id for worker in chat._active_runs.values()}
    for turn in chat.user_turn_widgets:
        if not getattr(turn, '_steering_message', None):
            continue
        locked = getattr(turn, '_steering_run_id', None) in active_ids
        for kind, tooltip in getattr(turn, '_steering_action_tooltips', {}).items():
            button = turn.actions_bar.buttons.get(kind)
            if button is not None:
                button.setEnabled(not locked)
                if locked:
                    set_text(button, 'ui.chat_steering.this_update_belongs_to_the_current_1ce8', setter='setToolTip')
                else:
                    bind_source(button, tooltip, setter='setToolTip')


def handle_event(chat, event, worker):
    if event.get('type') == 'user_update':
        # Остаток старого потока и результат уже запущенного tool остаются перед правкой.
        turn = render_update(chat, event, worker.run_id)
        turn._steering_waiting = True
        return
    if chat.active_thought_turn is not None:
        chat.active_thought_turn.finish_thinking()
    if chat.active_assistant_turn is not None:
        chat.active_assistant_turn.segment_kind = 'interim'
    chat.active_thought_turn = None
    chat.active_assistant_turn = None
    turn = render_update(chat, event, worker.run_id)
    if turn is not None:
        turn._steering_waiting = False


def rebuild_run(chat, worker, segments):
    from ui.views.chat_view import UserMessageTurn, ThoughtMessageTurn, AssistantMessageTurn
    from ui.views.chat_notifications import NotificationTurn
    from PySide6.QtCore import QPoint
    from shiboken6 import isValid
    from ui.qt_deferred import defer
    owned, expanded, anchor = [], {}, None
    bar = chat.scroll_area.verticalScrollBar()
    old_scroll = bar.value()
    follow = chat._auto_scroll_to_bottom
    navigation = getattr(chat, '_scroll_navigation_revision', 0)
    for index in range(chat.turns_layout.count()):
        turn = chat.turns_layout.itemAt(index).widget()
        if turn is None:
            continue
        identity = getattr(turn, 'chat_meta', {}).get('chat_id')
        if not follow and anchor is None:
            top = turn.mapTo(chat.scroll_area.viewport(), QPoint()).y()
            if top + turn.height() > 0:
                anchor = (identity, top)
        if ((getattr(turn, '_run_state', None) or {}).get('run_id') == worker.run_id
                or getattr(turn, '_steering_run_id', None) == worker.run_id):
            owned.append((index, turn))
            if isinstance(turn, ThoughtMessageTurn):
                expanded[identity] = turn.is_expanded
    start = min((index for index, _ in owned), default=chat.turns_layout.count() - 1)
    previous_insert = chat._generation_insert_index
    chat._thought_anchor_timer.stop()
    chat._thought_scroll_anchor = None
    chat._resume_target = None
    chat.active_thought_turn = None
    chat.active_assistant_turn = None
    for _, turn in owned:
        chat.turns_layout.removeWidget(turn)
        if turn in chat.user_turn_widgets:
            chat.user_turn_widgets.remove(turn)
        turn.hide()
        turn.deleteLater()
    try:
        chat._generation_insert_index = start
        chat._render_saved_run_segments(segments)
        stop = chat._generation_insert_index
        for index in range(start, stop):
            turn = chat.turns_layout.itemAt(index).widget()
            if isinstance(turn, (ThoughtMessageTurn, AssistantMessageTurn, NotificationTurn)):
                turn._run_state = dict(worker.result_state or {'run_id': worker.run_id, 'status': 'completed'})
            if isinstance(turn, ThoughtMessageTurn) and expanded.get(turn.chat_meta.get('chat_id')) and not turn.is_expanded:
                turn.toggle_collapse()
    finally:
        chat._generation_insert_index = previous_insert
    chat.user_turn_widgets[:] = [chat.turns_layout.itemAt(index).widget()
        for index in range(chat.turns_layout.count())
        if isinstance(chat.turns_layout.itemAt(index).widget(), UserMessageTurn)]
    chat.timeline_rail.update_turns(chat.user_turn_widgets)
    chat._auto_scroll_to_bottom = follow
    def settle():
        if not isValid(chat) or navigation != getattr(chat, '_scroll_navigation_revision', 0):
            return
        chat.turns_layout.activate()
        if follow and chat._auto_scroll_to_bottom:
            chat._follow_latest_content()
        elif not chat._auto_scroll_to_bottom:
            target = next((chat.turns_layout.itemAt(i).widget() for i in range(chat.turns_layout.count())
                if getattr(chat.turns_layout.itemAt(i).widget(), 'chat_meta', {}).get('chat_id') == anchor[0]), None) if anchor and anchor[0] else None
            if target is not None:
                offset = target.mapTo(chat.scroll_area.viewport(), QPoint()).y()
                bar.setValue(bar.value() + offset - anchor[1])
            else:
                bar.setValue(old_scroll)
    defer(0, chat, settle)
    defer(30, chat, settle)
