#!/usr/bin/env python3
"""
Точка входа для запуска GUI приложения.
Запускается независимо от основного сервера ядра агента.
"""
if __name__ == "__main__":
    # Отдельный GUI остаётся отзывчивым во время импортов и сборки главного окна.
    from ui.startup_process import start_startup_display, report_startup_phase, stop_startup_display
    start_startup_display()
    try:
        report_startup_phase('Подготовка хранилища и конфигурации…')
        from core.backups import register_process
        register_process()
        from core.log_session import session_log_path
        session_log_path(new=True)
        from ui.crash_report import enable_ui_fault_log
        enable_ui_fault_log()

        from core.paths import APP_PATHS
        APP_PATHS.ensure_element_dirs()
        from ui.startup_data import begin_startup_data, end_startup_data
        begin_startup_data(APP_PATHS)
        try:
            report_startup_phase('Загрузка библиотек интерфейса…')
            from ui.app import run_app
            run_app()
        finally:
            end_startup_data()
    finally:
        stop_startup_display()
