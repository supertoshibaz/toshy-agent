from PySide6.QtCore import QObject, Signal


class AppEventBus(QObject):
    """Шина глобальных сигналов для слабой связности модулей и интерфейса."""
    pipeline_open_requested = Signal(str)
    pipeline_resources_changed = Signal()
    context_usage_changed = Signal(str, str, object)
    chat_send_shortcut_changed = Signal()
    model_profiles_changed = Signal()
    session_model_changed = Signal(str)
    subagent_settings_requested = Signal(str)
    subagent_api_settings_requested = Signal(str)
    subagent_prompt_requested = Signal(str, object)
    mcp_changed = Signal()
    module_workspace_requested = Signal()
    open_file_requested = Signal(str)            # Запрос на открытие файла (абсолютный путь)
    open_markdown_anchor_requested = Signal(str, str)
    open_file_in_requested = Signal(str, str)    # Absolute path and explicit workspace target
    switch_view_tab_requested = Signal(int)      # Запрос переключения центральной вьюхи (0: Чат, 1: Файл, 2: Доска)
    module_status_changed = Signal(str, bool)    # Уведомление о смене статуса модуля (name, is_enabled)
    toggle_module_requested = Signal(str, str)   # Запрос на включение/выключение (name, category)
    file_renamed = Signal(str, str)              # Сигнал переименования файла (old_path, new_path)
    session_selected = Signal(str, str)     # Запрос на открытие сессии (session_id, branch_name)
    new_chat_requested = Signal()           # Сброс чата к созданию новой сессии
    new_chat_in_folder_requested = Signal(object)  # Новый несохранённый чат, будущая сессия попадёт в folder_id|None
    sessions_updated = Signal()             # Сигнал для обновления дерева сессий
    session_renamed = Signal(str, str)       # old_session_id, new_session_id
    branch_renamed = Signal(str, str, str)   # session_id, old_branch, new_branch
    workspace_view_changed = Signal(int)     # Активная центральная вкладка (0: Chat, 1: Projects, ...)
    session_memory_mode_changed = Signal(str, str)  # session_id, text|vector
    session_thoughts_context_changed = Signal(str, bool)  # session_id, include reasoning in model context
    session_replied_thoughts_context_changed = Signal(str, bool)  # session_id, include reasoning of replied assistant message
    task_status_check_requested = Signal(str, str, object)  # session_id, branch, in-progress tasks
    task_list_send_requested = Signal(str, str, object)  # session_id, branch, in-progress tasks

event_bus = AppEventBus()
